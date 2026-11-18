from backend.models import CompareConfig


def test_complete_book_configuration_preserves_all_339_report_pairs():
    pairs = [{"id": f"pair-{index}", "old_report_id": f"old-{index}",
              "new_report_id": f"new-{index}", "columns": []} for index in range(339)]
    config = CompareConfig.model_validate({"pairs": pairs})
    assert len(config.pairs) == 339
    assert config.pairs[-1].new_report_id == "new-338"
