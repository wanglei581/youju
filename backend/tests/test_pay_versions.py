"""开卖按小程序版本放开（D-074）：沙箱和测试名单只在开发版、体验版；现网只对代码包里带着定稿付费协议
版本号的小程序开放，老版本永远看不到价格。微信接口全部用 test_payments 的 FakeWechat。"""

import pytest
from pydantic import SecretStr
from starlette.datastructures import Headers

from tests.test_payments import HEADERS, create, setup_pay, submit  # noqa: F401
from youju import payments
from youju.config import Settings

RELEASE = {**HEADERS, "X-Youju-Env": "release"}
NO_CLIENT = {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": "pay-user"}


def test_client_of_reads_headers_and_treats_unknown_env_as_release():
    read = payments.client_of
    assert read(Headers({"x-youju-env": "Trial", "x-youju-pay-terms": " pay-x "})) == (
        payments.Client(terms="pay-x", env="trial")
    )
    assert read(Headers({"X-Youju-Env": "develop"})).env == "develop"
    # 认不出的环境、没带头的老版本：一律按正式版算（宁可看不到，不能多放）
    for env in ("release", "", "gray", "TRIAL "):
        expected = "trial" if env.strip().lower() == "trial" else "release"
        assert read(Headers({"X-Youju-Env": env})).env == expected
    assert read({}) == payments.Client(terms="", env="release")
    assert len(read({"X-Youju-Pay-Terms": "x" * 80}).terms) == 40


def _settings(**values) -> Settings:
    base = {
        "_env_file": None,
        "pay_enabled": True,
        "advisor_enabled": True,
        "pay_openids": "tester",
        "xpay_offer_id": "offer-test",
        "xpay_price_pass": 2900,
    }
    return Settings(**{**base, **values})


TRIAL_FINAL = payments.Client(terms="pay-final", env="trial")
RELEASE_FINAL = payments.Client(terms="pay-final", env="release")
RELEASE_OLD = payments.Client(terms="pay-2026-10-draft", env="release")


@pytest.mark.parametrize(
    ("env", "audience", "openid", "client", "expected"),
    [
        # 沙箱：只给测试名单、只在开发版和体验版；配成 all 也一样（沙箱付款不扣真钱）
        (1, "whitelist", "tester", TRIAL_FINAL, True),
        (1, "whitelist", "tester", payments.Client(env="develop"), True),
        (1, "whitelist", "tester", RELEASE_FINAL, False),
        (1, "whitelist", "stranger", TRIAL_FINAL, False),
        (1, "all", "stranger", TRIAL_FINAL, False),
        (1, "all", "tester", RELEASE_FINAL, False),
        # 现网 + 测试名单（受控真实付款）：测试人员、体验版、带定稿协议版本号，三样都要
        (0, "whitelist", "tester", TRIAL_FINAL, True),
        (0, "whitelist", "tester", RELEASE_FINAL, False),
        (0, "whitelist", "tester", payments.Client(terms="pay-old", env="trial"), False),
        (0, "whitelist", "stranger", TRIAL_FINAL, False),
        # 现网 + 所有人（开卖）：只看代码包里的协议版本号，不看是谁、不看环境
        (0, "all", "stranger", RELEASE_FINAL, True),
        (0, "all", None, RELEASE_FINAL, True),
        (0, "all", "tester", RELEASE_OLD, False),
        (0, "all", "tester", payments.Client(env="trial"), False),
    ],
)
def test_selling_to_by_build_and_terms(env, audience, openid, client, expected):
    settings = _settings(xpay_env=env, pay_audience=audience, pay_terms_version="pay-final")
    assert payments.selling_to(settings, openid, client) is expected


def test_live_sale_needs_final_terms_configured_even_if_client_sends_none():
    settings = _settings(xpay_env=0, pay_audience="all", pay_terms_version="")
    assert payments.selling_to(settings, "tester", payments.Client(env="release")) is False
    assert payments.selling_to(settings, "tester", payments.Client(env="trial")) is False


def test_release_build_never_shows_tester_store(setup_pay):  # noqa: F811
    """测试阶段（沙箱）：同一个测试人员在正式版里看不到商品、价格、购买入口，也下不了单；
    请求体不合法也是 404（中间件先挡），不暴露接口（D-074，运营规范 5.19）。"""
    client, settings, _ = setup_pay
    assert client.get("/api/v1/pay/products", headers=HEADERS).json()["items"]
    for headers in (RELEASE, NO_CLIENT):
        assert client.get("/api/v1/pay/products", headers=headers).status_code == 404
        assert client.get("/api/v1/me", headers=headers).json()["pay"]["enabled"] is False
        caps = client.get("/api/v1/advisor/capabilities", headers=headers).json()
        assert caps["pay"]["enabled"] is False
        bad = client.post("/api/v1/pay/orders", headers=headers, content=b"{")
        assert bad.status_code == 404 and bad.json()["detail"] == "没有找到接口"
    assert client.get("/api/v1/me", headers=HEADERS).json()["pay"]["enabled"] is True
    # 体验版照常能下单；正式版下单 404
    assert create(setup_pay).status_code == 201
    response = client.post(
        "/api/v1/pay/orders",
        headers=RELEASE,
        json={"product_id": "workbench_30d", "code": "c", "terms_version": "pay-test"},
    )
    assert response.status_code == 404


def test_live_sale_opens_only_to_packages_with_final_terms(setup_pay):  # noqa: F811
    """现网对所有人开卖：只有代码包里带着定稿协议版本号的小程序（开卖那一版）看得到价格、能下单；
    老版本（草稿号或没带）永远看不到（D-074：开卖要单独提审一版）。"""
    client, settings, _ = setup_pay
    settings.xpay_env, settings.pay_terms_version = 0, "pay-2026-12"
    settings.pay_invoice_type = "电子普通发票（测试）"
    settings.xpay_appkey_prod = SecretStr("12345")
    new = {**RELEASE, "X-Youju-Pay-Terms": "pay-2026-12"}
    old = {**RELEASE, "X-Youju-Pay-Terms": "pay-2026-10-draft"}
    stranger = {**new, "X-WX-OPENID": "someone-else"}
    for headers in (old, NO_CLIENT):
        assert client.get("/api/v1/pay/products", headers=headers).status_code == 404
        assert client.get("/api/v1/me", headers=headers).json()["pay"]["enabled"] is False
    listed = client.get("/api/v1/pay/products", headers=new).json()
    assert [i["productId"] for i in listed["items"]] == ["workbench_30d"]
    assert listed["sandbox"] is False
    # 对所有人：不在测试名单的人用新版本也看得到；看价格不要求登录
    assert client.get("/api/v1/pay/products", headers=stranger).json()["items"]
    no_login = {"X-Youju-Env": "release", "X-Youju-Pay-Terms": "pay-2026-12"}
    assert client.get("/api/v1/pay/products", headers=no_login).json()["items"]
    assert client.get("/api/v1/me", headers=new).json()["pay"]["enabled"] is True
    body = {"product_id": "workbench_30d", "code": "c", "terms_version": "pay-2026-12"}
    body["invoice_type"] = "电子普通发票（测试）"
    assert client.post("/api/v1/pay/orders", headers=old, json=body).status_code == 404
    assert client.post("/api/v1/pay/orders", headers=new, json=body).status_code == 201


def test_free_limit_message_offers_the_pass_only_to_builds_that_can_buy(setup_pay):  # noqa: F811
    """本月免费次数用完时，只有能买的小程序包才在提示里提批量工具；正式版（测试阶段）只说下月恢复。"""
    client, settings, _ = setup_pay
    for index in range(3):
        assert submit(setup_pay, key=f"free-{index}").status_code == 202
    trial = submit(setup_pay, key="over-trial")
    assert trial.status_code == 429 and "也可以买批量工具" in trial.json()["detail"]["message"]
    assert trial.json()["detail"]["canBuy"] is True
    release = client.post(
        "/api/v1/advisor/tasks",
        headers={**RELEASE, "Idempotency-Key": "over-release"},
        json={"kind": "compare", "rows": ["示例科技有限公司"]},
    )
    assert release.status_code == 429
    assert release.json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    assert "批量工具" not in release.json()["detail"]["message"]
    # 小程序据此丢掉之前缓存的价格（Codex 审阅）
    assert release.json()["detail"]["canBuy"] is False
