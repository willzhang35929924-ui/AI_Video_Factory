from dataclasses import dataclass
from typing import Any, Literal, Protocol

Record = dict[str, Any]
BackgroundKind = Literal["chatgpt_rpa", "manual", "local_comfyui", "other"]
PublishingAdapterKind = Literal["mock", "tiktok_direct_post", "tiktok_upload", "rpa"]
PUBLISHING_ADAPTER_KINDS: tuple[PublishingAdapterKind, ...] = (
    "mock", "tiktok_direct_post", "tiktok_upload", "rpa",
)


class CollectorProvider(Protocol):
    async def collect(self, task: Record, rule: Record) -> Record: ...


class ComfyUIProvider(Protocol):
    async def start_cutout(self, task: Record, source: Record) -> Record: ...


class BackgroundProvider(Protocol):
    async def generate_prompt(self, task: Record, settings: Record) -> Record: ...

    async def generate_image(self, task: Record, prompt: Record, provider: BackgroundKind) -> Record: ...


class VideoProcessor(Protocol):
    async def compose(self, task: Record, person: Record, background: Record, options: Record) -> Record: ...


class AIAnalyzer(Protocol):
    async def analyze(self, task: Record, product: Record) -> Record: ...


class PublishingProvider(Protocol):
    async def publish(self, task: Record, account: Record, publishing: Record) -> Record: ...


class MockCollectorProvider:
    async def collect(self, task: Record, rule: Record) -> Record:
        has_sample = rule.get("views", 0) > 0
        return {
            "task_id": task["id"],
            "provider": "MockCollectorProvider",
            "views": rule["views"] if has_sample else 128000,
            "likes": rule.get("likes", 0) if has_sample else 3420,
            "source_video": {
                "original_filename": f"{task['id']}-source.mp4",
                "source_url": None,
                "fps": 29.97,
                "width": 1080,
                "height": 1920,
                "duration": 24.6,
                "codec": "h264",
                "thumbnail": task.get("thumbnail", "/mock/portrait.svg"),
            },
        }


class MockComfyUIProvider:
    async def start_cutout(self, task: Record, source: Record) -> Record:
        return {
            "task_id": task["id"],
            "provider": "MockComfyUIProvider",
            "model": "Mock BiRefNet + MatAnyone2",
            "model_version": "mock-v1",
            "workflow_version": "mock-workflow-v1",
            "alpha_quality": 0.97,
            "temporal_stability": 0.96,
            "missing_body_score": 0.01,
            "flicker_score": 0.02,
            "intervals": [],
            "width": source.get("width", 1080),
            "height": source.get("height", 1920),
            "fps": source.get("fps", 29.97),
            "duration": source.get("duration", 24.6),
            "codec": "mock-alpha",
            "thumbnail": "/mock/portrait.svg",
        }


class MockBackgroundProvider:
    async def generate_prompt(self, task: Record, settings: Record) -> Record:
        scene = settings.get("scene_type", "现代生活空间")
        style = settings.get("style", "写实")
        lighting = settings.get("lighting", "自然柔光")
        color = settings.get("color", "中性暖白")
        aspect = settings.get("aspect_ratio", "9:16")
        product = settings.get("product_name", task["keyword"])
        return {
            "task_id": task["id"],
            "provider": settings.get("provider", "chatgpt_rpa"),
            "scene_type": scene,
            "style": style,
            "lighting": lighting,
            "color": color,
            "aspect_ratio": aspect,
            "prompt": (
                f"为{task['country']}市场的{product}制作{aspect}竖屏背景。"
                f"围绕关键词「{task['keyword']}」，使用{scene}场景、{style}风格、"
                f"{lighting}光照与{color}配色。画面中心和前景留白，"
                "为原视频人物及产品保留完整空间，透视与机位自然一致。"
            ),
            "negative_prompt": "人物，手，身体，商品，包装，品牌 Logo，字幕，水印，遮挡前景，错误透视，低清晰度",
        }

    async def generate_image(self, task: Record, prompt: Record, provider: BackgroundKind) -> Record:
        return {
            "task_id": task["id"],
            "provider": provider,
            "execution_provider": "MockBackgroundProvider",
            "prompt_id": prompt["id"],
            "width": 1080,
            "height": 1920,
            "fps": 0,
            "duration": 0,
            "codec": "png",
            "thumbnail": "/mock/room.svg",
            "image_filename": f"{task['id']}-background.png",
        }


class MockVideoProcessor:
    async def compose(self, task: Record, person: Record, background: Record, options: Record) -> Record:
        parameters = {
            "width": 1080, "height": 1920, "fps": 30,
            "exposure": 0, "contrast": 1, "saturation": 1, "crop": "center",
            **options,
        }
        return {
            "task_id": task["id"],
            "provider": "MockVideoProcessor",
            "person_asset_id": person["id"],
            "background_asset_id": background["id"],
            "parameters": parameters,
            "width": parameters["width"],
            "height": parameters["height"],
            "fps": parameters["fps"],
            "duration": person["duration"],
            "codec": "h264",
            "thumbnail": "/mock/portrait.svg",
        }


class MockAIAnalyzer:
    async def analyze(self, task: Record, product: Record) -> Record:
        return {
            "task_id": task["id"],
            "product_id": product["id"],
            "provider": "MockAIAnalyzer",
            "ai_match_score": 0.98,
            "product_match_score": 0.96,
            "overlay_detected": False,
            "watermark_detected": False,
            "subtitle_detected": False,
            "mask_flicker_score": 0.02,
            "missing_body_score": 0.01,
        }


class MockPublishingProvider:
    async def publish(self, task: Record, account: Record, publishing: Record) -> Record:
        return {
            "task_id": task["id"],
            "account_id": account["id"],
            "provider": "MockPublishingProvider",
            "adapter_kind": publishing.get("adapter_kind", "mock"),
            "receipt_id": "mock-" + publishing["id"],
            "status": "published",
            "platform_reference": f"mock://publishing/{publishing['id']}",
        }


@dataclass(frozen=True)
class MockProviders:
    collector: CollectorProvider
    comfyui: ComfyUIProvider
    background: BackgroundProvider
    processor: VideoProcessor
    analyzer: AIAnalyzer
    publishing: PublishingProvider


def get_mock_providers(app_mode: Literal["mock"] = "mock") -> MockProviders:
    if app_mode != "mock":
        raise ValueError("第一阶段所有 Provider 只支持 APP_MODE=mock")
    return MockProviders(
        collector=MockCollectorProvider(),
        comfyui=MockComfyUIProvider(),
        background=MockBackgroundProvider(),
        processor=MockVideoProcessor(),
        analyzer=MockAIAnalyzer(),
        publishing=MockPublishingProvider(),
    )
