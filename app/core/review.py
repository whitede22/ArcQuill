"""间隔重复复习（SM-2 的简化实现）。

状态推进是**纯函数**：不碰数据库、不碰时钟，于是每个分支都能用「喂一个状态、
断言一个结果」的方式测——这正是复习算法最容易写错的地方（答错却没重置、
记住了却没推进间隔、难度因子跌破下限）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 三档评分：记不住 / 拿不准 / 记住了。
FORGOT = 0
FUZZY = 1
REMEMBERED = 2

# 「太简单」不是第四个评分档，而是一个**归档信号**：用户判这个词不用再练，
# 它离开生词本和复习队列，但数据保留（见 `Repository.archive`）。
#
# 早先它是第四档，只把间隔拉长（4 天起、再乘 1.3 倍）。实测下来这条路不成立：
# 熟词每轮都还在队列里占一个位置，用户要么一路跳过它、要么真去评一次分，
# 两种反应都在浪费复习时间；而间隔再长也总会有下一次到期，队列只增不减。
#
# 数值保持 3，与历史统计里的 `easy` 事件对得上。
EASY = 3

# 难度因子（ease）的下限与初始值。ease 越低，间隔涨得越慢——越难记的词越常回来。
MIN_EASE = 1.3
INITIAL_EASE = 2.5

# 「记住了」的前两次间隔（天），之后每次乘 ease。
FIRST_INTERVAL = 1.0
SECOND_INTERVAL = 6.0

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def sm2(ease: float, interval: float, count: int, grade: int) -> tuple[float, float, int]:
    """按一次评分推进复习状态，返回 `(ease, interval, count)`。

    规则（SM-2 的简化，去掉标准版里没人用的 repetition 复刻）：

    - 忘了（FORGOT）：ease 扣 0.3，间隔回到 1 天，次数清零。
    - 模糊（FUZZY）：ease 扣 0.15，间隔回到 1 天，次数不变。
    - 记住了（REMEMBERED）：第一次间隔 1 天、第二次 6 天、之后每次乘 ease，次数 +1。

    **只接受这三档**。`EASY` 传进来会落到最后的 `ValueError`，这是刻意的：
    归档不走 SM-2，它不改变间隔，而是把词移出循环。让这里静默算出一个间隔，
    等于留下一条「归档了但还在队列里」的路径。
    """
    if grade == FORGOT:
        return max(MIN_EASE, ease - 0.3), FIRST_INTERVAL, 0
    if grade == FUZZY:
        return max(MIN_EASE, ease - 0.15), FIRST_INTERVAL, count
    if grade == REMEMBERED:
        if count == 0:
            new_interval = FIRST_INTERVAL
        elif count == 1:
            new_interval = SECOND_INTERVAL
        else:
            new_interval = interval * ease
        return ease, new_interval, count + 1
    raise ValueError(
        f"未知复习评分：{grade!r}（归档请走 Repository.archive，不进 SM-2）"
    )


def format_timestamp(dt: datetime) -> str:
    """把时刻格式化成与 `Entry.now()` 一致的字符串（可字典序比较）。"""
    return dt.strftime(_TIMESTAMP_FMT)


def due_timestamp(now: datetime, interval: float) -> str:
    """从当前时刻与间隔算出下一次复习的到期时间戳（同 `Entry.now()` 格式）。"""
    return format_timestamp(now + timedelta(days=interval))


def now_timestamp() -> str:
    return format_timestamp(datetime.now())
