import asyncio
import importlib
import json
import sys
import types

import pytest


def _load_spider(monkeypatch):
    fake_scraper = types.ModuleType("src.scraper")

    async def placeholder_scrape(task_config, debug_limit):
        return 0

    fake_scraper.scrape_xianyu = placeholder_scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)
    return importlib.import_module("spider_v2")


def _write_config(tmp_path, load_json_fixture):
    config_data = load_json_fixture("config.sample.json")
    # 两个同名任务：按名字选永远只跑第一个；按 id 才能选到第二个
    config_data[1]["task_name"] = config_data[0]["task_name"]
    config_data[1]["enabled"] = True
    for task in config_data:
        task["decision_mode"] = "keyword"
        task["keyword_rules"] = ["a7m4"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")
    return config_path, config_data


def _run(spider_v2, monkeypatch, tmp_path, argv):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))
    captured = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        captured.append(task_config)
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", *argv])
    asyncio.run(spider_v2.main())
    return captured


def test_task_id_selects_the_exact_task_even_when_names_collide(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_path, config_data = _write_config(tmp_path, load_json_fixture)

    captured = _run(spider_v2, monkeypatch, tmp_path, ["--config", str(config_path), "--task-id", "1"])

    assert len(captured) == 1
    assert captured[0]["id"] == 1
    assert captured[0]["keyword"] == config_data[1]["keyword"]


def test_task_name_still_works_and_picks_the_first_match(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_path, config_data = _write_config(tmp_path, load_json_fixture)

    captured = _run(
        spider_v2, monkeypatch, tmp_path, ["--config", str(config_path), "--task-name", config_data[0]["task_name"]]
    )

    assert [task["id"] for task in captured] == [0]


def test_unknown_task_id_runs_nothing(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_path, _ = _write_config(tmp_path, load_json_fixture)

    captured = _run(spider_v2, monkeypatch, tmp_path, ["--config", str(config_path), "--task-id", "7"])

    assert captured == []


def test_task_id_and_task_name_are_mutually_exclusive(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_path, _ = _write_config(tmp_path, load_json_fixture)

    with pytest.raises(SystemExit):
        _run(spider_v2, monkeypatch, tmp_path, ["--config", str(config_path), "--task-id", "1", "--task-name", "x"])


def test_tasks_loaded_from_sqlite_repository_are_plain_dicts(tmp_path, monkeypatch):
    """不带 --config 时任务来自 SqliteTaskRepository：Task 模型要经 model_dump() 转成 dict。"""
    from src.domain.models.task import Task

    spider_v2 = _load_spider(monkeypatch)

    class FakeRepository:
        async def find_all(self):
            return [
                Task(
                    id=0,
                    task_name="Repo task",
                    enabled=True,
                    keyword="thinkpad",
                    max_pages=1,
                    personal_only=False,
                    ai_prompt_base_file="prompts/base_prompt.txt",
                    ai_prompt_criteria_file="prompts/x_criteria.txt",
                    decision_mode="keyword",
                    keyword_rules=["t14"],
                )
            ]

    monkeypatch.setattr(spider_v2, "SqliteTaskRepository", FakeRepository)

    captured = _run(spider_v2, monkeypatch, tmp_path, ["--task-id", "0"])

    assert len(captured) == 1
    assert isinstance(captured[0], dict)
    assert captured[0]["id"] == 0
    assert captured[0]["task_name"] == "Repo task"
    assert captured[0]["keyword_rules"] == ["t14"]
