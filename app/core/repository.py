"""生词本仓储层：CRUD 与全文检索。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .db import (
    _CJK_RE,
    FTS_COLUMNS,
    FTS_WEIGHTS,
    connect,
    fts_values,
    has_fts,
    strip_cjk,
    to_bigram,
)
from .models import Entry, Phrase, split_pos, split_tags

# 学习状态的筛选项。空串是「全部」，同时充当 `query_entries` 的默认值。
#
# 只给这三个分面，不按「已复习 / 学习中」再切：SM-2 的间隔是连续的，任何
# 「学到哪一步」的细分都只是把同一个数字换几种说法，用户真正会问的是
# 「今天要复习哪些」与「我背好了哪些」。
STATE_ALL = ""
STATE_DUE = "due"
STATE_NEW = "new"
STATE_MASTERED = "mastered"
# 归档：复习时判过「太简单」，移出列表与复习队列，数据还在。
# 它是**第四个分面**而不是另一个入口：用户找「我标过太简单的词」时，
# 第一反应就是去状态下拉里翻，不会想到工具栏上另有一个按钮。
STATE_ARCHIVED = "archived"

STATE_LABELS: dict[str, str] = {
    STATE_ALL: "全部状态",
    STATE_DUE: "到期待复习",
    STATE_NEW: "还没复习过",
    STATE_MASTERED: "已掌握",
    STATE_ARCHIVED: "已归档",
}

# 「已掌握」的判据：SM-2 推出来的下次间隔超过三周。这不是一个严格的阈值，
# 而是 SM-2 自己的量级——初始间隔 1 天，每次乘 ease（2.5 起），大约四次「记住了」
# 就跨过 21 天。写死在这里而不是放进配置：它描述的是「多长算记住」，不该由
# 用户在设置里调，而调它的需求如果真出现，那时再加开关。
MASTERED_INTERVAL = 21.0


def _matches_state(
    due: str, count: int, interval: float, state: str, now: str, archived: str = ""
) -> bool:
    """学习状态的**唯一判据**。词条与数据库行都走这里。

    分成两个入口（`entry_matches_state` / `_row_matches_state`）却共用一份实现，
    是因为两个消费方的数据形态不同（数据类 vs sqlite3.Row），而判据必须只有
    一份：下拉里写「到期待复习 12」，点开却只有 9 条，用户会以为筛选坏了。

    归档在判据**之前**分流：归档过的词不属于「待复习」「已掌握」中的任何一档，
    它已经离开了学习循环。若只把它当第四档而不先从其余各档里排除，同一条词会
    同时出现在「已掌握」和「已归档」里，而两者语义相反。
    """
    if state == STATE_ARCHIVED:
        return bool(archived)
    if archived:
        return False
    if state == STATE_DUE:
        return bool(due) and due <= now
    if state == STATE_NEW:
        return int(count) == 0
    if state == STATE_MASTERED:
        return float(interval) >= MASTERED_INTERVAL
    return True


def entry_matches_state(entry: Entry, state: str, now: str) -> bool:
    """该词条是否落在指定的学习状态下。`state` 为空表示全部（不含归档）。"""
    return _matches_state(
        entry.review_due,
        entry.review_count,
        entry.review_interval,
        state,
        now,
        entry.archived_at,
    )


def _row_matches_state(row, state: str, now: str) -> bool:
    return _matches_state(
        row["review_due"] or "",
        int(row["review_count"] or 0),
        float(row["review_interval"] or 0.0),
        state,
        now,
        row["archived_at"] or "",
    )

_COLUMNS = (
    "id, lemma, phonetic, pos, definition_en, definition_zh, example, example_zh, "
    "synonyms_json, collocations_json, contextual_meaning, usage_note, notes, tags, "
    "source, prompt_version, "
    "created_at, updated_at, review_due, review_count, review_interval, review_ease, "
    "archived_at"
)

# JOINT 查询时 entries_fts 与 entries 存在同名列，必须全部加表前缀消歧义
_COLUMNS_E = ", ".join(f"e.{c.strip()}" for c in _COLUMNS.split(","))

ORDER_MAP = {
    "recent": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "alpha": "lemma COLLATE NOCASE ASC",
}

# 词组的排序映射。**单独一份而不是复用 ORDER_MAP**：那个的 alpha 按 lemma
# 排——phrases 表里根本没有这一列，复用会让「按字母排词组」直接抛 SQL 错误。
PHRASE_ORDER_MAP = {
    "recent": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "alpha": "phrase COLLATE NOCASE ASC",
}


def _tokens_lower(text: str) -> list[str]:
    """按词边界切开并小写化。用于「含这个词」的判断。"""
    return [t for t in re.split(r"[^0-9A-Za-z'\-]+", (text or "").lower()) if t]


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """`needle` 是否作为**连续的几个词**出现在 `haystack` 里。

    按词比较而不是按字符，是这一条的全部要点：`run` 不能命中 `trunk`
    与 `runway`。多词词组（`ice cream`）也照这条走——它在 `ice cream cone`
    里算命中，在 `icecream` 里不算。
    """
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))

# BM25 权重必须与 entries_fts 的列数严格一致，否则 bm25() 会直接报错。
# 由 FTS_WEIGHTS 生成，避免改列集时漏改这里。
_BM25 = "bm25(entries_fts, " + ", ".join(f"{w:.1f}" for w in FTS_WEIGHTS) + ")"

_FTS_INSERT = (
    f"INSERT INTO entries_fts(rowid, {', '.join(FTS_COLUMNS)}) "
    f"VALUES (?, {', '.join('?' * len(FTS_COLUMNS))})"
)


def _write_params(entry: Entry, now: str) -> tuple:
    """UPDATE 与 INSERT 共用的参数，顺序与两条语句的前 16 个占位列一致。

    两条语句的列序必须同步改：这里少一项或多一项，写进去的就不是错位而是
    sqlite3 的绑定数量异常，错误信息还落在调用行上，看不出是这里少写了一个。
    """
    return (
        entry.lemma,
        entry.phonetic,
        entry.pos,
        entry.definition_en,
        entry.definition_zh,
        entry.example,
        entry.example_zh,
        entry.synonyms_json(),
        entry.collocations_json(),
        entry.contextual_meaning,
        entry.usage_note,
        entry.notes,
        entry.tags,
        entry.source,
        entry.prompt_version,
        now,
    )


def build_fts_query(raw: str) -> str:
    """把用户输入翻译为 FTS5 查询表达式。

    - 英文部分：裸词 + 前缀匹配。裸词（不带列名前缀）在 FTS5 中会命中**所有列**，
      因此英文侧天然覆盖例句、备注、标签，无需逐列罗列；
      列名前缀只用于给 lemma / 释义 / 同义语加权重提示。
    - 中文部分：转 bigram 后以 phrase 匹配 zh_bigram 列，实现子串语义。
      zh_bigram 聚合了全部中文内容（释义、例句翻译、备注、标签、同义语简释）。
    - 中英混合时以 AND 连接，纯中文或纯英文则单通道
    """
    q = (raw or "").strip()
    if not q:
        return ""

    ascii_part = strip_cjk(q).strip()
    cjk_segs = _CJK_RE.findall(q)
    clauses: list[str] = []

    if ascii_part:
        words = [w for w in re.split(r"[^0-9A-Za-z'\-]+", ascii_part) if w]
        if words:
            terms = " OR ".join('"{}"*'.format(w.replace('"', "")) for w in words)
            clauses.append(
                "({terms} OR lemma:({terms}) OR definition_en:({terms}) "
                "OR synonyms_text:({terms}))".format(terms=terms)
            )

    if cjk_segs:
        subs = []
        for seg in cjk_segs:
            bg = to_bigram(seg)
            if bg:
                subs.append('zh_bigram:"{}"'.format(bg))
        if subs:
            clauses.append("(" + " OR ".join(subs) + ")")

    return " AND ".join(clauses)


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """从连接本身问出库文件路径。

    不新增构造参数是有意的：`Repository(conn)` 的调用点散在主程序、自检、
    冒烟、消融各处，只要有一处忘了传路径，那条路径上的后台任务就会静默退回
    共用主连接——而这正是要消灭的写法。让 SQLite 自己回答（`database_list`
    的第一行就是主库文件），不存在「调用方记错」的余地。

    内存库的文件名为空串，此处映射成 None，`clone()` 据此拒绝：另开一条连接
    拿到的是另一个空库，给它一条「连得上但什么都没有」的连接，比直接报错更
    难查。
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return Path(row[2]) if row[2] else None
    except sqlite3.Error:
        return None
    return None


class Repository:
    """所有数据库读写都经由本类，UI 层不直接接触 SQL。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.db_path = _conn_db_path(conn)

    def clone(self) -> Repository:
        """为当前线程另开一条连接，返回指向同一个库的新仓储。

        后台任务（查词、重新查询、导入）一律走这个入口。主线程那条连接有线程
        归属，worker 里直接用它会在 CPython 上抛 ProgrammingError；而把
        `check_same_thread` 放宽成 False 换来的是「不报错但事务互相踩」。

        新连接是独立事务边界：调用方写完后必须自己 commit（仓储的写方法都已
        自行提交），用完 `close()`。克隆不复制任何状态，两个实例之间除库文件
        外没有其他联系。
        """
        if self.db_path is None:
            raise RuntimeError("这条连接没有对应的库文件，无法为其他线程另开连接")
        return Repository(connect(self.db_path))

    def close(self) -> None:
        """关闭底层连接。只用于 `clone()` 出来的临时仓储。"""
        self.conn.close()

    def upsert(self, entry: Entry) -> Entry:
        """写入词条；已存在的同 lemma 词条按其 id 覆盖全部内容字段。

        **用户自有字段（notes / tags）的边界**：`entry.id` 为空说明这是「刚从接口
        取回的新内容」，而不是用户编辑过的对象。此时若库中已有同 lemma 的词条，
        必须保留库里的备注与标签——否则点一次「重新查询」就会把用户写的备注静默
        抹掉（全文索引一起失效），且无法恢复。反过来，用户先 `get` 再改再 `upsert`
        （读取-修改-写入）时 `id` 已就绪，走「全部字段生效」分支，因此把备注清空
        这类显式操作依然有效。

        **整体成败一致**：写主表与写索引必须一起成功或一起失败。SQLite 的隐式
        事务在异常时不会自动回滚，于是出错后连接会停在 `in_transaction=True`
        的状态上，而**下一次任意成功操作的 commit() 会把这份半成品一并提交**
        ——后果是「主表里有这个词条、索引里没有」，用户搜索时找不到它，且
        期间被 `_sync_fts` 的 DELETE 波及的**别的词条**索引也会一起丢。
        这里显式回滚，把「写坏了」变成「这次没写成」。
        """
        now = Entry.now()
        # 必须在把 row 里的 id 写回 entry 之前判断，否则这个信息就丢了
        fresh_content = entry.id is None
        cur = self.conn.cursor()
        try:
            row = self._fetch_existing(cur, entry, fresh_content)
            params = _write_params(entry, now)
            if row:
                self._update_row(cur, entry, row, params, now)
            else:
                self._insert_row(cur, entry, params, now)
            self._sync_fts(entry)
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return entry

    def _fetch_existing(self, cur, entry: Entry, fresh_content: bool):
        """取库里同 lemma 的行，必要时把用户自有字段回写到 entry 上。

        `fresh_content` 由调用方在回写 id 之前算好：它表达的是「这次写入带来的
        是新内容」，不是「库里有没有这一行」。两者在读取-修改-写入场景下不同。
        """
        row = cur.execute(
            "SELECT id, created_at, notes, tags, contextual_meaning "
            "FROM entries WHERE lemma = ?",
            (entry.lemma,),
        ).fetchone()
        if row and fresh_content:
            # 接口回填：用户字段以库中现有值为准，并回写到内存对象上，
            # 让界面渲染与索引同步都能看到「保留之后」的内容
            entry.notes = row["notes"] or ""
            entry.tags = row["tags"] or ""
            # 语境解释是**上一次查词时贴过的上下文**派生出来的。重查路径不带
            # 上下文（见 `DictionaryService.refresh`），新内容里这一栏必然是空的；
            # 直接覆盖等于用户点了「重新查询」就弄丢了刚看到的语境义，而界面上
            # 没有任何解释。所以「新内容没给」时保留旧值——换了上下文再查时
            # 新值非空，照常覆盖，这条规则不会挡住真正的更新。
            if not (entry.contextual_meaning or "").strip():
                entry.contextual_meaning = row["contextual_meaning"] or ""
        return row

    def _update_row(
        self, cur, entry: Entry, row, params: tuple, now: str
    ) -> None:
        """覆盖已有行。id 与 created_at 取自库，更新时间取本次。

        `archived_at` 在这里被清空，等于「写入即复活」：用户重新查一个曾经判过
        「太简单」的词，或者回去给它补一条备注，都是在说「我现在要这个词」。
        让归档状态挡在这类操作前面，用户会看到「改完就消失了」，而界面上没有
        任何地方解释它去了哪。
        """
        entry.id = row["id"]
        entry.created_at = row["created_at"]
        entry.updated_at = now
        cur.execute(
            """UPDATE entries SET lemma=?, phonetic=?, pos=?, definition_en=?,
               definition_zh=?, example=?, example_zh=?, synonyms_json=?,
               collocations_json=?, contextual_meaning=?, usage_note=?,
               notes=?, tags=?, source=?, prompt_version=?,
               archived_at='', updated_at=? WHERE id=?""",
            params + (entry.id,),
        )

    def _insert_row(self, cur, entry: Entry, params: tuple, now: str) -> None:
        """插入新行，并在这里播种复习计划。"""
        entry.created_at = now
        entry.updated_at = now
        # 复习计划在这里「播种」：新词条第一次进入生词本时，把它排进
        # 「待复习」（review_due = now，立即到期）。这对应产品口径
        # 「查词即收藏」——收藏的下一步就是复习，第一次复习（评分）
        # 之后才由 SM-2 决定下一次间隔。
        #
        # 而撤销删除恢复的旧词条带着**复习历史**（review_count > 0 或
        # review_due 非空），照原样保留——撤销不该重置记忆进度。所以
        # 判据是「进来的 Entry 有没有复习历史」，而不是「这行是不是新建」。
        seed_due = (
            entry.review_due
            if (entry.review_due or entry.review_count > 0)
            else now
        )
        cur.execute(
            """INSERT INTO entries
               (lemma, phonetic, pos, definition_en, definition_zh, example,
                example_zh, synonyms_json, collocations_json, contextual_meaning,
                usage_note, notes, tags, source,
                prompt_version, updated_at, created_at, review_due,
                review_count, review_interval, review_ease)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            params
            + (
                now,
                seed_due,
                entry.review_count,
                entry.review_interval,
                entry.review_ease,
            ),
        )
        entry.id = cur.lastrowid

    def _sync_fts(self, entry: Entry) -> None:
        """FTS 索引手工同步（索引表与 entries 表松耦合，便于整体重建）。

        行的构造交给 db.fts_values()，与旧库重建共用同一份逻辑。
        """
        if not has_fts(self.conn):
            return  # FTS5 不可用，检索走 LIKE 回退
        self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry.id,))
        self.conn.execute(_FTS_INSERT, (entry.id, *fts_values(entry)))

    def get(self, lemma: str) -> Entry | None:
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM entries WHERE lemma = ?", (lemma,)
        ).fetchone()
        return Entry.from_row(row) if row else None

    def get_by_id(self, entry_id: int) -> Entry | None:
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        entry = Entry.from_row(row)
        # 详情视图需要展示收录的变体形式
        entry.aliases = self.aliases_of(entry.id)
        return entry

    def delete(self, entry_id: int) -> None:
        """删除词条及其索引。

        三条 DELETE 必须同生共死：索引表若正好坏着，第二条会抛错，而第一条
        已经把主表那行删掉了——SQLite 的隐式事务不会自动回滚，于是连接停在
        未提交状态，下一次成功操作的 commit() 会让「主表删了、索引还在」或
        反之落盘。显式回滚把这种情况收敛成「这次没删成」。

        第三条 aliases 是冗余的（DDL 里对 entries 有 ON DELETE CASCADE，
        且 `connect()` 开了 foreign_keys），保留它是为了让「删除要清哪几张表」
        在这一个地方看得全，不依赖读者去别处翻外键定义。
        """
        try:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
            self.conn.execute("DELETE FROM aliases WHERE entry_id = ?", (entry_id,))
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    # ---------- 别名（屈折形式）映射 ----------

    def link_alias(self, alias: str, entry_id: int) -> None:
        """记录「某形态 == 某词条」。alias 表主键唯一，重复写入即忽略。"""
        alias = (alias or "").strip().lower()
        if not alias or entry_id is None:
            return
        self.conn.execute(
            """INSERT INTO aliases(alias, entry_id, created_at) VALUES(?,?,?)
               ON CONFLICT(alias) DO UPDATE SET entry_id=excluded.entry_id""",
            (alias, entry_id, Entry.now()),
        )
        self.conn.commit()

    def find_by_alias(self, alias: str) -> Entry | None:
        row = self.conn.execute(
            f"""SELECT {_COLUMNS_E} FROM aliases a
                JOIN entries e ON e.id = a.entry_id
                WHERE a.alias = ?""",
            ((alias or "").strip().lower(),),
        ).fetchone()
        return Entry.from_row(row) if row else None

    def aliases_of(self, entry_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT alias FROM aliases WHERE entry_id = ? ORDER BY alias", (entry_id,)
        ).fetchall()
        return [r["alias"] for r in rows]

    def count(self) -> int:
        """生词本条数。**不含归档**：归档的词已经离开生词本，把它算进来会让
        「共 300 条」与列表里能数出来的条数对不上。"""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE archived_at = ''"
            ).fetchone()["c"]
        )

    def count_archived(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE archived_at != ''"
            ).fetchone()["c"]
        )

    def all_ids(self) -> set[int]:
        """全库词条 id 的快照（**含归档**）。

        给批量导入用：区分「本次新增」与「本来就有」只能靠 id——`lookup()`
        返回的「是否命中缓存」说明不了这件事（别名命中 running → run 也是
        缓存命中，但那条词早就存在了）。导入前取一次快照，比每个词 import
        之前再查一遍库便宜得多，也不会被「同一个词在文件里出现两次」骗到。

        归档的词必须算「本来就有」：表上 lemma 唯一，把它当新词去插会直接
        撞唯一约束；而它在导入语义里确实存在，只是用户暂时不想复习。
        """
        return {int(r["id"]) for r in self.conn.execute("SELECT id FROM entries")}

    def list_entries(
        self,
        order: str = "recent",
        limit: int | None = None,
        offset: int = 0,
        archived: bool = False,
    ) -> list[Entry]:
        """列词条。`archived=False`（默认）只要生词本里的，True 只要归档的。

        没有「两者都要」这一档：界面上「已归档」是独立的一个筛选态，
        把两类混在同一屏里，用户分不清哪条还该复习。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM entries WHERE archived_at "
            f"{'!=' if archived else '='} '' "
            f"ORDER BY {ORDER_MAP.get(order, ORDER_MAP['recent'])}"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (limit, offset)
        return [Entry.from_row(r) for r in self.conn.execute(sql, params)]

    def entries_added_between(
        self,
        start: str = "",
        end: str = "",
        *,
        archived: bool = False,
        strict_start: bool = False,
    ) -> list[Entry]:
        """按**添加时间**取词条，空串表示该端不限。

        `start` / `end` 是 `YYYY-MM-DD HH:MM:SS`，与 `created_at` 同一种格式。
        比较走字符串字典序：格式定长且高位在前，字典序就是时间序。这是 SQLite
        没有日期类型的直接后果，也是为什么配置里那个 `last_export_at` 必须
        用 `_coerce_timestamp` 卡住格式——两边格式不一致时比较不会报错，只会
        悄悄多给或少给一批词。

        `end` 是闭区间：用户选「9 月 30 日」，指的是那一整天，所以调用方把
        日期补成 `23:59:59` 再传进来，这里不猜。

        `strict_start` 决定 `start` 是 `>=` 还是 `>`。「自上次导出以来」必须
        用 `>`：上次导出那一刻创建的词已经在那份文件里了，`>=` 会把它再导一次，
        而重复的卡片导进 Anki 之后要人工挑出来。
        """
        clauses = ["archived_at " + ("!= ''" if archived else "= ''")]
        params: list[str] = []
        if start:
            clauses.append("created_at > ?" if strict_start else "created_at >= ?")
            params.append(start)
        if end:
            clauses.append("created_at <= ?")
            params.append(end)
        sql = (
            f"SELECT {_COLUMNS} FROM entries WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC, id ASC"
        )
        return [Entry.from_row(r) for r in self.conn.execute(sql, tuple(params))]

    def earliest_created(self) -> str:
        """库里最早一条的添加时间，空库返回空串。用于日期区间的默认起点。"""
        row = self.conn.execute(
            "SELECT MIN(created_at) AS m FROM entries WHERE archived_at = ''"
        ).fetchone()
        return (row["m"] or "") if row else ""

    # ---------- 筛选（学习状态 / 词性） ----------
    #
    # 「这一屏显示哪些词条」有四个独立条件：关键字、标签、学习状态、词性。
    # 它们**全部在 Python 侧做**，不在 SQL 里拼条件，理由有两条：
    #
    #   1. 标签与词性是**多值字段**（`tags` 是空格分隔的一串、`pos` 可能是
    #      "adj., n."）。SQL 只能子串匹配，而子串匹配一定会在「某个值包含
    #      另一个」时出错：筛 `adj` 会把 `adv` 排除，筛 `考研` 会带出 `考研英语`。
    #      判对错的那一半必须走 `split_tags` / `split_pos` 这两份唯一解析。
    #   2. 四个条件互相叠加时，SQL 得按 2×2×2 组合拼语句；任何一个分支写错，
    #      表现都是「列表空着但不报错」。
    #
    # 代价是全量读进内存。生词本是用户一个一个攒的，量级在千条以内，读一遍
    # 几十毫秒；而界面只在「重载列表」时读一次，滚动加载用的是缓存好的结果
    # （见 `MainWindow._ensure_visible_entries`）。

    def query_entries(
        self,
        *,
        order: str = "recent",
        state: str = "",
        pos: str = "",
        tag: str = "",
        keyword: str = "",
        now: str = "",
    ) -> list[Entry]:
        """按四个条件过滤并排序，返回全量结果（分页由调用方切片）。

        `now` 只用于「到期待复习」的判定，由调用方传入——仓储层不读时钟，
        与 `due_count` / `due_entries` 一致，测试才好写。
        """
        query = (keyword or "").strip()
        # 归档态是一个**互斥的视图**，不是叠加条件：选了「已归档」时列表只
        # 出自归档池，其余状态同理。数据源先按这个分流，再走状态判据。
        want_archived = state == STATE_ARCHIVED
        if query:
            # 走检索时保持 BM25 序：用户此刻要的是「最相关的那个」，而不是
            # 「最近加的」。与旧行为一致。
            entries = self.search(query, limit=None, archived=want_archived)
        else:
            entries = self.list_entries(
                order=order, limit=None, archived=want_archived
            )

        if state:
            entries = [e for e in entries if entry_matches_state(e, state, now)]
        if pos:
            entries = [e for e in entries if pos in split_pos(e.pos)]
        if tag:
            entries = [e for e in entries if tag in e.tag_list]
        return entries

    def state_counts(self, now: str) -> dict[str, int]:
        """各学习状态下有多少条。用于下拉里的「待复习（12）」这种写法。

        整表扫一遍在 Python 侧数，而不是四条 SQL：判据必须与 `query_entries`
        用的是同一份（`entry_matches_state`），否则下拉写「12 条」而点开是 9 条，
        用户会以为筛选坏了。
        """
        counts = {
            STATE_ALL: 0,
            STATE_DUE: 0,
            STATE_NEW: 0,
            STATE_MASTERED: 0,
            STATE_ARCHIVED: 0,
        }
        for row in self.conn.execute(
            "SELECT review_due, review_count, review_interval, archived_at "
            "FROM entries"
        ):
            # 「全部状态」也走同一份判据，而不是无条件 +1：归档的词不属于
            # 「全部」，否则下拉写「全部状态（20）」、点开只有 18 条。
            for key in (STATE_ALL, STATE_DUE, STATE_NEW, STATE_MASTERED, STATE_ARCHIVED):
                if _row_matches_state(row, key, now):
                    counts[key] += 1
        return counts

    def pos_counts(self) -> list[tuple[str, int]]:
        """全库词性及其词条数，按数量降序、同数量按名称升序。

        与 `tag_counts` 同款：解析只走 `split_pos` 那一份实现，GROUP BY 会把
        「adj., n.」当成一个整体。
        """
        counts: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT pos FROM entries WHERE archived_at = ''"
        ):
            for item in split_pos(row["pos"]):
                counts[item] = counts.get(item, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    # ---------- 标签 ----------

    def tag_counts(self) -> list[tuple[str, int]]:
        """全库标签及其词条数，按数量降序、同数量按名称升序。

        **标签的解析只有一份实现**（`models.split_tags`），所以这里不能写
        `GROUP BY tags`——那是把「一个字段」当成「一个值」来数，而它实际上是
        一串用空格分开的标签。用 SQL 分组的话，「考研 高频」与「考研」会被算成
        两个不同的标签，而它们在用户眼里是同一个「考研」。

        词条数决定降序：界面上用来筛选的标签，最常用的那个最该先看到。
        """
        counts: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT tags FROM entries WHERE archived_at = ''"
        ):
            for tag in split_tags(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def list_by_tag(
        self, tag: str, order: str = "recent", limit: int | None = None
    ) -> list[Entry]:
        """列出带某个标签的词条。

        先用 `LIKE` 在 SQL 侧粗筛，再用 `Entry.tag_list` 在 Python 侧精判：
        精判必须走**同一份解析**，否则筛「考研」会把「考研英语」也带进来
        （`LIKE '%考研%'` 只是子串匹配，它对标签边界一无所知）。

        粗筛的作用只是把候选行缩小——少了它就要把全库读进内存；而少了精判，
        结果就是错的。两者都不能省，且判断对错的那一半只有精判。
        """
        clean = (tag or "").strip()
        if not clean:
            return self.list_entries(order=order, limit=limit)
        sql = f"SELECT {_COLUMNS} FROM entries WHERE tags LIKE ?"
        params: tuple = (f"%{clean}%",)
        entries = [
            entry
            for entry in (
                Entry.from_row(r) for r in self.conn.execute(sql, params)
            )
            if clean in entry.tag_list
        ]
        if order == "alpha":
            entries.sort(key=lambda e: e.lemma.lower())
        else:
            entries.sort(
                key=lambda e: (e.created_at, e.id or 0), reverse=order != "oldest"
            )
        return entries if limit is None else entries[:limit]

    def search(
        self, query: str, limit: int | None = 300, *, archived: bool = False
    ) -> list[Entry]:
        """中英双通道检索，FTS 不可用时回退 LIKE。

        `limit=None` 表示不设上限（导出路径使用）。列表需要上限是为了界面
        性能，但打印稿若沿用同一上限会被静默截断——文件头还自称「共 N 条」，
        而 N 恰好是被砍掉之后的数量，用户无从察觉。

        `archived` 与 `list_entries` 同义：默认只在生词本范围内检索。这一点
        必须显式写成参数而不是靠调用方事后过滤——FTS 分支带 LIMIT，过滤发生在
        截断之后，会把「前 300 条里的归档词」滤掉一部分，剩下的不足 300 条，
        而用户看不出这里少过东西。
        """
        query = (query or "").strip()
        if not query:
            return self.list_entries(limit=limit, archived=archived)

        expr = build_fts_query(query)
        if expr and has_fts(self.conn):
            try:
                scope = "e.archived_at != ''" if archived else "e.archived_at = ''"
                sql = f"""SELECT {_COLUMNS_E} FROM entries_fts f
                        JOIN entries e ON e.id = f.rowid
                        WHERE entries_fts MATCH ? AND {scope}
                        ORDER BY {_BM25}"""
                params: tuple = (expr,)
                if limit is not None:
                    sql += " LIMIT ?"
                    params = (expr, limit)
                rows = self.conn.execute(sql, params).fetchall()
                if rows:
                    return [Entry.from_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass

        # 回退路径的字段覆盖必须与 FTS 一致，否则「索引一坏，检索能力静默退化」，
        # 这种退化极难被察觉，因此这里逐项对齐 notes / example / tags。
        like = f"%{query}%"
        scope = "archived_at != ''" if archived else "archived_at = ''"
        sql = f"""SELECT {_COLUMNS} FROM entries
                WHERE ({scope}) AND (
                    lemma LIKE ? OR definition_en LIKE ? OR definition_zh LIKE ?
                   OR example LIKE ? OR example_zh LIKE ? OR synonyms_json LIKE ?
                   OR notes LIKE ? OR tags LIKE ?)
                ORDER BY created_at DESC"""
        params = (like,) * 8 + (() if limit is None else (limit,))
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.conn.execute(sql, params).fetchall()
        return [Entry.from_row(r) for r in rows]

    # ---------- 词组收藏 ----------
    #
    # 与 entries 是**两张独立的表**（理由见 db.DDL 里 phrases 的建表注释）。
    # 这一节的每个方法都只碰 phrases：两张表的 AUTOINCREMENT 各自从 1 发号，
    # 同一个数字在两边指的是两条毫不相干的数据。任何「拿词条的 id 去删词组」
    # 或反过来，都会安静地删掉另一样东西。

    def add_phrase(self, phrase: Phrase) -> Phrase:
        """收藏一条词组；**同 phrase 已存在时返回已有的那条，不覆盖它的内容**。

        去重落在数据库的 `UNIQUE` 约束上（`ON CONFLICT DO NOTHING`），不是
        「先查一次再决定写不写」：那是两次独立操作，中间隔着一次可能失败的
        时间，而且是「同一件事两处判」。

        **不覆盖已有内容**：同一个词组从两个词条里收藏时，两次带的简释与例句
        可能不同。以先收藏的那份为准——与本程序「用户已有的内容不被后来的
        写入盖掉」一致。否则用户手动改过的那条会被下一次收藏静默改写，
        而两次操作在界面上都显示「已收藏」。
        """
        phrase.phrase = (phrase.phrase or "").strip()
        if not phrase.phrase:
            raise ValueError("词组内容不能为空")
        cur = self.conn.execute(
            """INSERT INTO phrases
               (phrase, gloss_zh, example, example_zh, source_lemma, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (
                phrase.phrase,
                phrase.gloss_zh,
                phrase.example,
                phrase.example_zh,
                phrase.source_lemma,
                Entry.now(),
            ),
        )
        self.conn.commit()
        stored = self.get_phrase(phrase.phrase)
        if stored is not None:
            return stored
        # 理论上到不了这里（要么插进去、要么撞上已有那条）。真到了说明
        # `ON CONFLICT` 没有按预期生效，此时把内存对象原样返回并补上本次的
        # 行号，好过返回 None 让调用方炸在别处。
        phrase.id = cur.lastrowid
        return phrase

    def get_phrase(self, phrase: str) -> Phrase | None:
        """按内容取一条（大小写不敏感，与表上的 COLLATE NOCASE 同款）。"""
        row = self.conn.execute(
            "SELECT * FROM phrases WHERE phrase = ?", ((phrase or "").strip(),)
        ).fetchone()
        return Phrase.from_row(row) if row else None

    def get_phrase_by_id(self, phrase_id: int) -> Phrase | None:
        row = self.conn.execute(
            "SELECT * FROM phrases WHERE id = ?", (int(phrase_id),)
        ).fetchone()
        return Phrase.from_row(row) if row else None

    def list_phrases(
        self, order: str = "recent", limit: int | None = None
    ) -> list[Phrase]:
        """列出全部词组。`limit=None` 表示不设上限（导出路径使用）。"""
        sql = (
            "SELECT * FROM phrases ORDER BY "
            f"{PHRASE_ORDER_MAP.get(order, PHRASE_ORDER_MAP['recent'])}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            return [
                Phrase.from_row(r)
                for r in self.conn.execute(sql, (int(limit),))
            ]
        return [Phrase.from_row(r) for r in self.conn.execute(sql)]

    def search_phrases(
        self, query: str, order: str = "recent", limit: int | None = None
    ) -> list[Phrase]:
        """按关键字过滤词组收藏。**刻意不建第二套全文索引。**

        判据是「四个字段里任意一个包含这个子串（忽略大小写）」，在 Python 侧
        比较，不进 SQL：
          1. 词组是用户**一条一条攒**出来的，量级是几十，全读进来比一次子串
             匹配快得多，也没有任何索引能帮上忙（前缀树在这个规模没有意义）；
          2. 少一张 FTS 表就少一套「列集与主表对不上时整表重建」的逻辑——
             那套逻辑在 entries 上必须存在，在 phrases 上纯属自找。
        """
        keyword = (query or "").strip().lower()
        items = self.list_phrases(order=order)
        if keyword:
            items = [
                item
                for item in items
                if keyword
                in " ".join(
                    (item.phrase, item.gloss_zh, item.example, item.example_zh)
                ).lower()
            ]
        return items if limit is None else items[:limit]

    def delete_phrase(self, phrase_id: int) -> bool:
        """删除一条词组，返回是否真的删掉了。"""
        cur = self.conn.execute("DELETE FROM phrases WHERE id = ?", (int(phrase_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def count_phrases(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS c FROM phrases").fetchone()["c"]
        )

    def phrases_containing(self, lemma: str) -> list[str]:
        """列出「含这个词」的已收藏词组，供详情区回显。

        判据是**按词边界逐词比较**（见 `_contains_run`），不是 `LIKE '%词%'`：
        子串匹配一定会误伤——`run` 会命中 `trunk`、`runway`、`brunch`，而用户
        看到的是「这个词底下凭空冒出一串不相干的词组」，且没有任何提示说明
        那是子串匹配的结果。
        """
        needle = _tokens_lower(lemma)
        if not needle:
            return []
        return [
            item.phrase
            for item in self.list_phrases()
            if _contains_run(_tokens_lower(item.phrase), needle)
        ]

    # ---------- 间隔重复复习 ----------

    def due_count(self, now: str) -> int:
        """到期待复习的条数（`review_due` 非空且不晚于 now）。不含归档。"""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM entries "
                "WHERE archived_at = '' AND review_due != '' AND review_due <= ?",
                (now,),
            ).fetchone()["c"]
        )

    def due_entries(self, now: str, limit: int | None = None) -> list[Entry]:
        """到期待复习的词条，按到期先后排列（最早到期的最先复习）。

        `limit=None` 表示不限条数（复习会话一次过完所有到期的词）；与
        `list_entries` / `search` 同款，默认不设上限。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM entries "
            "WHERE archived_at = '' AND review_due != '' AND review_due <= ? "
            "ORDER BY review_due ASC, id ASC"
        )
        params: tuple = (now,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (now, limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [Entry.from_row(r) for r in rows]

    def apply_review(self, entry_id: int, grade: int, now: datetime) -> None:
        """按一次评分推进该词条的复习状态。

        只更新四列复习字段与 `updated_at`，不碰词条内容——复习是「学习进度」，
        不是「内容变更」。

        `now` 是**`datetime` 对象**（不是字符串）：它要用来算「下一次到期 = now +
        间隔」。与 `due_count` / `due_entries` 的字符串形参刻意不同——那两个只是
        拿字符串与库里存的 `review_due` 做字典序比较，这里要拿它做日期运算。
        由调用方传入，仓储层不读时钟，便于测试。
        """
        from .review import due_timestamp, format_timestamp, sm2

        row = self.conn.execute(
            "SELECT review_ease, review_interval, review_count, archived_at "
            "FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return
        if row["archived_at"]:
            # 归档的词已经离开学习循环。给它推进间隔没有任何可见效果
            # （它不在列表也不在队列里），只会让日后放回来时带着一个
            # 谁也算不清的到期日。
            return
        ease, interval, count = sm2(
            float(row["review_ease"] or 2.5),
            float(row["review_interval"] or 0.0),
            int(row["review_count"] or 0),
            grade,
        )
        # 与 `upsert` 同一条理由：异常后连接会停在 `in_transaction=True`，
        # 下一次任意成功操作的 commit() 会把这次的半成品一并提交。显式回滚，
        # 把「写坏了」变成「这次没写成」。
        try:
            self.conn.execute(
                "UPDATE entries SET review_due = ?, review_interval = ?, "
                "review_count = ?, review_ease = ?, updated_at = ? WHERE id = ?",
                (due_timestamp(now, interval), interval, count, ease, format_timestamp(now), entry_id),
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def archive(self, entry_id: int, now: datetime) -> bool:
        """把词条移出生词本（复习里判「太简单」）。返回是否真的改到了行。

        同时清空 `review_due`：三重过滤是有意的。`due_count` / `due_entries`
        已经排除了归档行，但复习队列、待复习角标、自检的到期判据各有各的入口，
        任何一处将来漏了 `archived_at` 条件，归档的词就会重新冒出来——而它
        最不该做的就是重新冒出来。把 `review_due` 一并清掉，那个漏网的查询
        也查不到它。

        `WHERE archived_at = ''` 让重复归档返回 False，而不是把时间戳刷新一遍：
        调用方靠这个返回值判断「这一下真的改变状态了吗」。
        """
        from .review import format_timestamp

        stamp = format_timestamp(now)
        try:
            cur = self.conn.execute(
                "UPDATE entries SET archived_at = ?, review_due = '', updated_at = ? "
                "WHERE id = ? AND archived_at = ''",
                (stamp, stamp, entry_id),
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return cur.rowcount > 0

    def unarchive(self, entry_id: int, now: datetime) -> bool:
        """把归档的词放回生词本，并重新排进复习队列（立即到期）。

        `review_interval` 清零、`review_ease` 与 `review_count` 保留：
        放回来意味着「我又想学了」，那就从短间隔重新爬；而清除 ease 等于
        忘掉这个词以前有多难，清除 count 等于假装从没学过，两者都会让
        下次评分给出一个不合适的间隔。
        """
        from .review import format_timestamp

        stamp = format_timestamp(now)
        try:
            cur = self.conn.execute(
                "UPDATE entries SET archived_at = '', review_due = ?, "
                "review_interval = 0, updated_at = ? "
                "WHERE id = ? AND archived_at != ''",
                (stamp, stamp, entry_id),
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return cur.rowcount > 0
