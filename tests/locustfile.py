from locust import HttpUser, task, between
import random

class ShopFastUser(HttpUser):
    wait_time = between(0.05, 0.2)
    headers = {"Authorization": "Bearer demo-token-shopfast"}

    @task(3)
    def get_recurrente(self):
        uid = random.choice(["U10", "U22", "U35", "U48"])
        self.client.get(f"/v1/recommendations?usuario={uid}&contexto=electronica&use_cache=true", headers=self.headers, name="/v1/recommendations [recurrente]")

    @task(2)
    def get_nuevo(self):
        uid = f"U_NEW_{random.randint(10000, 99999)}"
        self.client.get(f"/v1/recommendations?usuario={uid}&contexto=audio&use_cache=true", headers=self.headers, name="/v1/recommendations [nuevo]")
