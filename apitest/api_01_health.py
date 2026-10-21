"""接口测试：健康检查（无需鉴权）。"""
from _client import endpoint

SPEC = endpoint(
    name="健康检查",
    method="GET",
    path="/health",
    auth=False,
    expect=(200,),
    note="免登录、不重定向，返回服务与数据库状态",
)
