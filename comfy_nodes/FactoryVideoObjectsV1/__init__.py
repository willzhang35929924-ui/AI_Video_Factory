"""Native SAM3 multi-object tracking with stable Factory object identities."""

import json

import torch

import comfy.model_management
import comfy.utils
from comfy.ldm.sam3.tracker import pack_masks
from comfy_extras.nodes_sam3 import SAM3_TrackToMask


class FactorySAM3VideoTrackV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "images": ("IMAGE",),
            "model": ("MODEL",),
            "initial_masks": ("MASK",),
            "object_ids": ("STRING", {"default": "person_1,bag_1"}),
            "direction": (["forward", "backward"], {"default": "forward"}),
        }}

    RETURN_TYPES = ("SAM3_TRACK_DATA",)
    RETURN_NAMES = ("object_tracks",)
    FUNCTION = "track"
    CATEGORY = "Factory/video objects"

    def track(self, images, model, initial_masks, object_ids, direction="forward"):
        identities = [value.strip() for value in object_ids.split(",") if value.strip()]
        if len(identities) != initial_masks.shape[0] or len(set(identities)) != len(identities):
            raise ValueError("Each initial mask requires one unique object_id")
        frame_count, height, width, _ = images.shape
        ordered_images = images.flip(0) if direction == "backward" else images
        comfy.model_management.load_model_gpu(model)
        device = comfy.model_management.get_torch_device()
        dtype = model.model.get_dtype()
        result = model.model.diffusion_model.forward_video(
            images=ordered_images[..., :3].movedim(-1, 1),
            initial_masks=initial_masks.unsqueeze(1).to(device=device, dtype=dtype),
            pbar=comfy.utils.ProgressBar(frame_count),
            max_objects=len(identities), target_device=device, target_dtype=dtype,
        )
        # Native SAM3 returns logits; native SAM3.1 already returns packed track data.
        if isinstance(result, torch.Tensor):
            packed = pack_masks(result > 0).to(comfy.model_management.intermediate_device())
            result = {"packed_masks": packed, "n_frames": frame_count}
        else:
            result = dict(result)
        if direction == "backward" and result["packed_masks"] is not None:
            result["packed_masks"] = result["packed_masks"].flip(0)
        result["orig_size"] = (height, width)
        result["object_ids"] = identities
        result["direction"] = direction
        result["mask_semantics"] = "object segmentation; not fine matting alpha"
        return (result,)


class FactorySAM3ObjectMaskV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "object_tracks": ("SAM3_TRACK_DATA",),
            "object_ids": ("STRING", {"default": "bag_1"}),
        }}

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("selected_object_mask",)
    FUNCTION = "select"
    CATEGORY = "Factory/video objects"

    def select(self, object_tracks, object_ids):
        identities = object_tracks["object_ids"]
        selected = [value.strip() for value in object_ids.split(",") if value.strip()]
        missing = [value for value in selected if value not in identities]
        if missing:
            raise ValueError("Unknown object_ids: " + json.dumps(missing, ensure_ascii=False))
        indices = ",".join(str(identities.index(value)) for value in selected)
        if not selected:
            raise ValueError("Select at least one object_id")
        output = SAM3_TrackToMask.execute(object_tracks, object_indices=indices)
        return (output.result[0],)


NODE_CLASS_MAPPINGS = {
    "FactorySAM3VideoTrackV1": FactorySAM3VideoTrackV1,
    "FactorySAM3ObjectMaskV1": FactorySAM3ObjectMaskV1,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "FactorySAM3VideoTrackV1": "Factory SAM3 Video Objects v1",
    "FactorySAM3ObjectMaskV1": "Factory SAM3 Select Object v1",
}
