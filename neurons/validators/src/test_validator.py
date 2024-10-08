from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor, as_completed

from validator import app

client = TestClient(app)


def send_post_request():
    response = client.post(
        "/miner_request",
        json={
            "miner_hotkey": "5EHgHZBfx4ZwU7GzGCS8VCMBLBEKo5eaCvXKiu6SASwWT6UY",
            "miner_address": "localhost",
            "miner_port": 8000
        },
    )
    assert response.status_code == 200

def test_socket_connections():
    num_requests = 10  # Number of simultaneous requests
    with ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = [executor.submit(send_post_request) for _ in range(num_requests)]
        
        for future in as_completed(futures):
            response = future.result()
            assert response.status_code == 200

if __name__ == "__main__":
    test_socket_connections()