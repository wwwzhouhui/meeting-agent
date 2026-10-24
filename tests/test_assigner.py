from src.agents.task_agent.assigner import Assigner
from src.shared.schema import Participant

PARTICIPANTS = [
    Participant(user_id="u-zhangsan", name="张三"),
    Participant(user_id="u-lisi", name="李四"),
    Participant(user_id="u-wangwu", name="王五"),
]


def test_exact_name():
    owner_id, conf = Assigner().resolve_owner("李四", PARTICIPANTS, "完成证书续期")
    assert owner_id == "u-lisi"
    assert conf >= 0.9


def test_alias():
    owner_id, conf = Assigner().resolve_owner("小王", PARTICIPANTS, "提交灰度方案")
    assert owner_id == "u-wangwu"
    assert conf == 0.85


def test_module_owner_fallback():
    owner_id, conf = Assigner().resolve_owner("", PARTICIPANTS, "支付证书处理")
    assert owner_id == "u-lisi"
    assert conf == 0.75


def test_unknown_forces_human():
    owner_id, conf = Assigner().resolve_owner("赵六", PARTICIPANTS, "做点杂事")
    assert owner_id is None
    assert conf < 0.7
