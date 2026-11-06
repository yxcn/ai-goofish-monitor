from src.scraper import ItemDetailUnavailable, LoginRequiredError, RiskControlError, classify_detail_ret


def test_classify_detail_ret_recognizes_success_and_failures():
    assert classify_detail_ret(["SUCCESS::调用成功"]) == "ok"
    assert classify_detail_ret("SUCCESS::ok") == "ok"
    assert classify_detail_ret(["FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦"]) == "risk_control"
    assert classify_detail_ret(["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]) == "login_expired"
    assert classify_detail_ret(["FAIL_SYS_SESSION_EXPIRED::Session过期"]) == "login_expired"
    assert classify_detail_ret(["FAIL_SYS_TOKEN_EMPTY::令牌为空"]) == "login_expired"
    assert classify_detail_ret(["FAIL_BIZ_ITEM_NOT_FOUND::商品不存在"]) == "failed"
    assert classify_detail_ret([]) == "failed"
    assert classify_detail_ret(None) == "failed"


def test_control_flow_errors_are_distinct_exception_types():
    # 商品级 except 链依赖这三类互不继承，才能让风控/登录失效穿透到任务层
    assert not issubclass(ItemDetailUnavailable, (RiskControlError, LoginRequiredError))
    assert not issubclass(RiskControlError, LoginRequiredError)
