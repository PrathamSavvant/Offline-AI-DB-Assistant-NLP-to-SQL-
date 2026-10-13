import pytest

from acceptance.live_fk_acceptance import main


@pytest.mark.live
def test_real_llama_mysql_production_path_acceptance():
    assert main(["--fail-on-failure"]) == 0
