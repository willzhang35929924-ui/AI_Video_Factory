from fastapi import APIRouter

from app.integrations.publishing.schemas import AccountConfirmation, AccountControl, AccountInput, BatchInput, ControlInput, DescriptionReuse, ProductInput, PublishConfirmation, TaskInput
from app.integrations.publishing.service import PublishingService


def create_publishing_router(repository, config):
    router = APIRouter(prefix="/api/publishing", tags=["real-publishing"])
    service = PublishingService(repository, config)
    router.service = service

    @router.get("/config")
    def configuration():
        return config.public()

    @router.get("/products")
    def products():
        return service.products()

    @router.post("/products", status_code=201)
    def create_product(payload: ProductInput):
        return service.save_product(payload)

    @router.put("/products/{product_id}")
    def update_product(product_id: str, payload: ProductInput):
        return service.save_product(payload, product_id)

    @router.get("/products/{product_id}/descriptions/reuse-options")
    def description_reuse_options(product_id: str):
        return service.description_reuse_options(product_id)

    @router.post("/products/{product_id}/descriptions/reuse")
    def reuse_descriptions(product_id: str, payload: DescriptionReuse):
        return service.reuse_descriptions(product_id, payload)

    @router.delete("/products/{product_id}")
    def delete_product(product_id: str):
        return service.delete_product(product_id)

    @router.get("/accounts")
    def accounts():
        return service.accounts()

    @router.post("/accounts", status_code=201)
    def create_account(payload: AccountInput):
        return service.save_account(payload)

    @router.put("/accounts/{account_id}")
    def update_account(account_id: str, payload: AccountInput):
        return service.save_account(payload, account_id)

    @router.patch("/accounts/{account_id}")
    def control_account(account_id: str, payload: AccountControl):
        return service.control_account(account_id, payload.enabled)

    @router.post("/accounts/{account_id}/confirm")
    def confirm_account(account_id: str, payload: AccountConfirmation):
        return service.confirm_account(account_id, payload)

    @router.delete("/accounts/{account_id}")
    def delete_account(account_id: str):
        return service.delete_account(account_id)

    @router.get("/tasks")
    def tasks():
        return service.tasks()

    @router.post("/tasks", status_code=201)
    def create_task(payload: TaskInput):
        return service.create_task(payload)

    @router.get("/tasks/{task_id}")
    def task(task_id: str):
        return service.task(task_id)

    @router.get("/tasks/{task_id}/preflight")
    async def preflight(task_id: str):
        return await service.preflight(task_id)

    @router.post("/tasks/{task_id}/retry")
    async def retry(task_id: str):
        return await service.retry(task_id)

    @router.post("/tasks/{task_id}/reconcile-upload")
    async def reconcile_upload(task_id: str):
        return await service.reconcile_upload(task_id)

    @router.post("/tasks/{task_id}/reconcile-product")
    async def reconcile_product(task_id: str):
        return await service.reconcile_product(task_id)

    @router.get("/tasks/{task_id}/submit-confirmation")
    def submit_confirmation(task_id: str):
        return service.submit_confirmation(task_id)

    @router.post("/tasks/{task_id}/confirm-submit", status_code=202)
    async def confirm_submit(task_id: str, payload: PublishConfirmation):
        return await service.confirm_submit(task_id, payload)

    @router.post("/tasks/{task_id}/reconcile-submit")
    async def reconcile_submit(task_id: str):
        return await service.reconcile_submit(task_id)

    @router.post("/tasks/{task_id}/observe-product")
    async def observe_product(task_id: str):
        return await service.observe_product(task_id)

    @router.get("/batches")
    def batches():
        return service.batches()

    @router.post("/batches", status_code=201)
    async def create_batch(payload: BatchInput):
        return await service.create_batch(payload.task_ids)

    @router.get("/queue")
    def queue():
        return service.queue()

    @router.post("/control")
    async def control(payload: ControlInput):
        return await service.control(payload.batch_id, payload.action)

    return router
