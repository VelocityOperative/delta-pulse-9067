"""ProductHunt 内部 (frontend) GraphQL 查询。

使用精简查询（只取需要的字段），相比页面原始查询响应体积减少 60%+。
服务端通过 APQ (Automatic Persisted Queries) 校验，
请求时必须带 extensions.persistedQuery.sha256Hash（即查询文本的 sha256）。
"""
import hashlib

OPERATION_NAMES = {
    "daily": "LeaderboardDailyPage",
    "weekly": "LeaderboardWeeklyPage",
    "monthly": "LeaderboardMonthlyPage",
}

_POST_FIELDS = (
    "id slug name tagline(respectEmbargo:true) shortenedUrl "
    "dailyRank weeklyRank monthlyRank latestScore commentsCount "
    "featuredAt createdAt thumbnailImageUuid "
    "topics(first:3){edges{node{id name}}} "
    "product{id slug websiteUrl}"
)

_BODY = ("{edges{node{__typename ... on Post{" + _POST_FIELDS + "}}}"
         "pageInfo{endCursor hasNextPage}}")

_QUERIES = {
    "daily": (
        "query LeaderboardDailyPage($year:Int!$month:Int!$day:Int!"
        "$cursor:String$order:PostsOrder$featured:Boolean!)"
        "{homefeedItems(featured:$featured first:20 year:$year month:$month "
        "day:$day order:$order after:$cursor)" + _BODY + "}"),
    "weekly": (
        "query LeaderboardWeeklyPage($year:Int!$week:Int!"
        "$cursor:String$order:PostsOrder$featured:Boolean!)"
        "{homefeedItems(featured:$featured first:20 year:$year week:$week "
        "order:$order after:$cursor)" + _BODY + "}"),
    "monthly": (
        "query LeaderboardMonthlyPage($year:Int!$month:Int!"
        "$cursor:String$order:PostsOrder$featured:Boolean!)"
        "{homefeedItems(featured:$featured first:20 year:$year month:$month "
        "order:$order after:$cursor)" + _BODY + "}"),
}


def get_query(period: str) -> str:
    return _QUERIES[period]


def build_payload(period: str, variables: dict) -> dict:
    query = get_query(period)
    return {
        "operationName": OPERATION_NAMES[period],
        "variables": variables,
        "query": query,
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": hashlib.sha256(query.encode()).hexdigest(),
            }
        },
    }


def build_variables(period: str, *, year: int, month: int | None = None,
                    day: int | None = None, week: int | None = None,
                    cursor: str | None = None, featured: bool = False) -> dict:
    v: dict = {"featured": featured, "year": year, "order": "VOTES", "cursor": cursor}
    if period == "daily":
        v.update({"month": month, "day": day})
    elif period == "weekly":
        v.update({"week": week})
    elif period == "monthly":
        v.update({"month": month})
    else:
        raise ValueError(period)
    return v
