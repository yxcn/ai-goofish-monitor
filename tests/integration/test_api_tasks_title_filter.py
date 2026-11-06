def test_title_include_keywords_round_trip_through_api(api_client, sample_task_payload):
    payload = {**sample_task_payload, "title_include_keywords": "a7m4, A7M4, 全画幅"}
    response = api_client.post("/api/tasks/", json=payload)
    assert response.status_code == 200
    assert response.json()["task"]["title_include_keywords"] == ["a7m4", "全画幅"]

    # 只改价格时不应碰标题预筛选
    response = api_client.patch("/api/tasks/0", json={"max_price": "20000"})
    assert response.status_code == 200
    assert response.json()["task"]["title_include_keywords"] == ["a7m4", "全画幅"]

    response = api_client.patch("/api/tasks/0", json={"title_include_keywords": []})
    assert response.status_code == 200
    assert response.json()["task"]["title_include_keywords"] == []

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json()[0]["title_include_keywords"] == []


def test_task_without_title_keywords_defaults_to_empty_list(api_client, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200
    assert response.json()["task"]["title_include_keywords"] == []
