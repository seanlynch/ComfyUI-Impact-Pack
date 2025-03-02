import os
import sys

import folder_paths
import comfy.samplers
import comfy.sd
import warnings
from segment_anything import sam_model_registry
from io import BytesIO
import piexif
import zipfile
import re

import impact.wildcards

from impact.utils import *
import impact.core as core
from impact.core import SEG
from impact.config import MAX_RESOLUTION, latent_letter_path
from PIL import Image
import numpy as np
import hashlib
import json
import safetensors.torch
from PIL.PngImagePlugin import PngInfo
import comfy.model_management

warnings.filterwarnings('ignore', category=UserWarning, message='TypedStorage is deprecated')

model_path = folder_paths.models_dir


# Nodes
# folder_paths.supported_pt_extensions
folder_paths.folder_names_and_paths["mmdets_bbox"] = ([os.path.join(model_path, "mmdets", "bbox")], folder_paths.supported_pt_extensions)
folder_paths.folder_names_and_paths["mmdets_segm"] = ([os.path.join(model_path, "mmdets", "segm")], folder_paths.supported_pt_extensions)
folder_paths.folder_names_and_paths["mmdets"] = ([os.path.join(model_path, "mmdets")], folder_paths.supported_pt_extensions)
folder_paths.folder_names_and_paths["sams"] = ([os.path.join(model_path, "sams")], folder_paths.supported_pt_extensions)
folder_paths.folder_names_and_paths["onnx"] = ([os.path.join(model_path, "onnx")], {'.onnx'})


class ONNXDetectorProvider:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"model_name": (folder_paths.get_filename_list("onnx"), )}}

    RETURN_TYPES = ("ONNX_DETECTOR", )
    FUNCTION = "load_onnx"

    CATEGORY = "ImpactPack"

    def load_onnx(self, model_name):
        model = folder_paths.get_full_path("onnx", model_name)
        return (core.ONNXDetector(model), )


class CLIPSegDetectorProvider:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "text": ("STRING", {"multiline": False}),
                        "blur": ("FLOAT", {"min": 0, "max": 15, "step": 0.1, "default": 7}),
                        "threshold": ("FLOAT", {"min": 0, "max": 1, "step": 0.05, "default": 0.4}),
                        "dilation_factor": ("INT", {"min": 0, "max": 10, "step": 1, "default": 4}),
                    }
                }

    RETURN_TYPES = ("BBOX_DETECTOR", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, text, blur, threshold, dilation_factor):
        if "CLIPSeg" in nodes.NODE_CLASS_MAPPINGS:
            return (core.BBoxDetectorBasedOnCLIPSeg(text, blur, threshold, dilation_factor), )
        else:
            print("[ERROR] CLIPSegToBboxDetector: CLIPSeg custom node isn't installed. You must install biegert/ComfyUI-CLIPSeg extension to use this node.")


class SAMLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model_name": (folder_paths.get_filename_list("sams"), ),
                "device_mode": (["AUTO", "Prefer GPU", "CPU"],),
            }
        }

    RETURN_TYPES = ("SAM_MODEL", )
    FUNCTION = "load_model"

    CATEGORY = "ImpactPack"

    def load_model(self, model_name, device_mode="auto"):
        modelname = folder_paths.get_full_path("sams", model_name)

        if 'vit_h' in model_name:
            model_kind = 'vit_h'
        elif 'vit_l' in model_name:
            model_kind = 'vit_l'
        else:
            model_kind = 'vit_b'

        sam = sam_model_registry[model_kind](checkpoint=modelname)
        # Unless user explicitly wants to use CPU, we use GPU
        device = comfy.model_management.get_torch_device() if device_mode == "Prefer GPU" else "CPU"

        if device_mode == "Prefer GPU":
            sam.to(device=device)

        sam.is_auto_mode = device_mode == "AUTO"

        print(f"Loads SAM model: {modelname} (device:{device_mode})")
        return (sam, )


class ONNXDetectorForEach:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "onnx_detector": ("ONNX_DETECTOR",),
                    "image": ("IMAGE",),
                    "threshold": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.01}),
                    "dilation": ("INT", {"default": 10, "min": -512, "max": 512, "step": 1}),
                    "crop_factor": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 10, "step": 0.1}),
                    "drop_size": ("INT", {"min": 1, "max": MAX_RESOLUTION, "step": 1, "default": 10}),
                    }
                }

    RETURN_TYPES = ("SEGS", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detector"

    OUTPUT_NODE = True

    def doit(self, onnx_detector, image, threshold, dilation, crop_factor, drop_size):
        segs = onnx_detector.detect(image, threshold, dilation, crop_factor, drop_size)
        return (segs, )


class DetailerForEach:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "segs": ("SEGS", ),
                     "model": ("MODEL",),
                     "clip": ("CLIP",),
                     "vae": ("VAE",),
                     "guide_size": ("FLOAT", {"default": 256, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "guide_size_for": ("BOOLEAN", {"default": True, "label_on": "bbox", "label_off": "crop_region"}),
                     "max_size": ("FLOAT", {"default": 768, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                     "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                     "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                     "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                     "positive": ("CONDITIONING",),
                     "negative": ("CONDITIONING",),
                     "denoise": ("FLOAT", {"default": 0.5, "min": 0.0001, "max": 1.0, "step": 0.01}),
                     "feather": ("INT", {"default": 5, "min": 0, "max": 100, "step": 1}),
                     "noise_mask": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "force_inpaint": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "wildcard": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                     },
                "optional": {"detailer_hook": ("DETAILER_HOOK",), }
                }

    RETURN_TYPES = ("IMAGE", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    @staticmethod
    def do_detail(image, segs, model, clip, vae, guide_size, guide_size_for_bbox, max_size, seed, steps, cfg, sampler_name, scheduler,
                  positive, negative, denoise, feather, noise_mask, force_inpaint, wildcard_opt=None, detailer_hook=None,
                  refiner_ratio=None, refiner_model=None, refiner_clip=None, refiner_positive=None, refiner_negative=None):

        image_pil = tensor2pil(image).convert('RGBA')

        enhanced_alpha_list = []
        enhanced_list = []
        cropped_list = []
        cnet_pil_list = []

        for seg in segs[1]:
            cropped_image = seg.cropped_image if seg.cropped_image is not None \
                                              else crop_ndarray4(image.numpy(), seg.crop_region)

            mask_pil = feather_mask(seg.cropped_mask, feather)

            is_mask_all_zeros = (seg.cropped_mask == 0).all().item()
            if is_mask_all_zeros:
                print(f"Detailer: segment skip [empty mask]")
                continue

            if noise_mask:
                cropped_mask = seg.cropped_mask
            else:
                cropped_mask = None

            enhanced_pil, cnet_pil = core.enhance_detail(cropped_image, model, clip, vae, guide_size, guide_size_for_bbox, max_size,
                                                         seg.bbox, seed, steps, cfg, sampler_name, scheduler,
                                                         positive, negative, denoise, cropped_mask, force_inpaint, wildcard_opt, detailer_hook,
                                                         refiner_ratio=refiner_ratio, refiner_model=refiner_model,
                                                         refiner_clip=refiner_clip, refiner_positive=refiner_positive,
                                                         refiner_negative=refiner_negative, control_net_wrapper=seg.control_net_wrapper)

            if cnet_pil is not None:
                cnet_pil_list.append(cnet_pil)

            if not (enhanced_pil is None):
                # don't latent composite-> converting to latent caused poor quality
                # use image paste
                image_pil.paste(enhanced_pil, (seg.crop_region[0], seg.crop_region[1]), mask_pil)
                enhanced_list.append(pil2tensor(enhanced_pil))

            if not (enhanced_pil is None):
                # Convert enhanced_pil_alpha to RGBA mode
                enhanced_pil_alpha = enhanced_pil.copy().convert('RGBA')

                # Apply the mask
                mask_array = seg.cropped_mask.astype(np.uint8) * 255
                mask_image = Image.fromarray(mask_array, mode='L').resize(enhanced_pil_alpha.size)
                enhanced_pil_alpha.putalpha(mask_image)
                enhanced_alpha_list.append(pil2tensor(enhanced_pil_alpha))

            cropped_list.append(torch.from_numpy(cropped_image))

        image_tensor = pil2tensor(image_pil.convert('RGB'))

        cropped_list.sort(key=lambda x: x.shape, reverse=True)
        enhanced_list.sort(key=lambda x: x.shape, reverse=True)
        enhanced_alpha_list.sort(key=lambda x: x.shape, reverse=True)

        return image_tensor, cropped_list, enhanced_list, enhanced_alpha_list, cnet_pil_list

    def doit(self, image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name,
             scheduler, positive, negative, denoise, feather, noise_mask, force_inpaint, wildcard, detailer_hook=None):

        enhanced_img, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list = \
            DetailerForEach.do_detail(image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps,
                                      cfg, sampler_name, scheduler, positive, negative, denoise, feather, noise_mask,
                                      force_inpaint, wildcard, detailer_hook)

        return (enhanced_img, )


class DetailerForEachPipe:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "segs": ("SEGS", ),
                     "guide_size": ("FLOAT", {"default": 256, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "guide_size_for": ("BOOLEAN", {"default": True, "label_on": "bbox", "label_off": "crop_region"}),
                     "max_size": ("FLOAT", {"default": 768, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                     "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                     "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                     "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                     "denoise": ("FLOAT", {"default": 0.5, "min": 0.0001, "max": 1.0, "step": 0.01}),
                     "feather": ("INT", {"default": 5, "min": 0, "max": 100, "step": 1}),
                     "noise_mask": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "force_inpaint": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "basic_pipe": ("BASIC_PIPE", ),
                     "wildcard": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                     "refiner_ratio": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0}),
                     },
                "optional": {
                    "detailer_hook": ("DETAILER_HOOK",),
                    "refiner_basic_pipe_opt": ("BASIC_PIPE",),
                    }
                }

    RETURN_TYPES = ("IMAGE", "SEGS", "BASIC_PIPE", "IMAGE")
    RETURN_NAMES = ("image", "segs", "basic_pipe", "cnet_images")
    OUTPUT_IS_LIST = (False, False, False, True)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    def doit(self, image, segs, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
             denoise, feather, noise_mask, force_inpaint, basic_pipe, wildcard, refiner_ratio=None, detailer_hook=None, refiner_basic_pipe_opt=None):

        model, clip, vae, positive, negative = basic_pipe

        if refiner_basic_pipe_opt is None:
            refiner_model, refiner_clip, refiner_positive, refiner_negative = None, None, None, None
        else:
            refiner_model, refiner_clip, _, refiner_positive, refiner_negative = refiner_basic_pipe_opt

        enhanced_img, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list = \
            DetailerForEach.do_detail(image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg,
                                      sampler_name, scheduler, positive, negative, denoise, feather, noise_mask,
                                      force_inpaint, wildcard, detailer_hook,
                                      refiner_ratio=refiner_ratio, refiner_model=refiner_model,
                                      refiner_clip=refiner_clip, refiner_positive=refiner_positive, refiner_negative=refiner_negative)

        # set fallback image
        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return (enhanced_img, segs, basic_pipe, cnet_pil_list)


class FaceDetailer:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "model": ("MODEL",),
                     "clip": ("CLIP",),
                     "vae": ("VAE",),
                     "guide_size": ("FLOAT", {"default": 256, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "guide_size_for": ("BOOLEAN", {"default": True, "label_on": "bbox", "label_off": "crop_region"}),
                     "max_size": ("FLOAT", {"default": 768, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                     "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                     "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                     "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                     "positive": ("CONDITIONING",),
                     "negative": ("CONDITIONING",),
                     "denoise": ("FLOAT", {"default": 0.5, "min": 0.0001, "max": 1.0, "step": 0.01}),
                     "feather": ("INT", {"default": 5, "min": 0, "max": 100, "step": 1}),
                     "noise_mask": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "force_inpaint": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),

                     "bbox_threshold": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "bbox_dilation": ("INT", {"default": 10, "min": -512, "max": 512, "step": 1}),
                     "bbox_crop_factor": ("FLOAT", {"default": 3.0, "min": 1.0, "max": 10, "step": 0.1}),

                     "sam_detection_hint": (["center-1", "horizontal-2", "vertical-2", "rect-4", "diamond-4", "mask-area", "mask-points", "mask-point-bbox", "none"],),
                     "sam_dilation": ("INT", {"default": 0, "min": -512, "max": 512, "step": 1}),
                     "sam_threshold": ("FLOAT", {"default": 0.93, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "sam_bbox_expansion": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                     "sam_mask_hint_threshold": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "sam_mask_hint_use_negative": (["False", "Small", "Outter"],),

                     "drop_size": ("INT", {"min": 1, "max": MAX_RESOLUTION, "step": 1, "default": 10}),

                     "bbox_detector": ("BBOX_DETECTOR", ),
                     "wildcard": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                     },
                "optional": {
                    "sam_model_opt": ("SAM_MODEL", ),
                    "segm_detector_opt": ("SEGM_DETECTOR", ),
                    "detailer_hook": ("DETAILER_HOOK",)
                }}

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "MASK", "DETAILER_PIPE", "IMAGE")
    RETURN_NAMES = ("image", "cropped_refined", "cropped_enhanced_alpha", "mask", "detailer_pipe", "cnet_images")
    OUTPUT_IS_LIST = (False, True, True, False, False, True)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Simple"

    @staticmethod
    def enhance_face(image, model, clip, vae, guide_size, guide_size_for_bbox, max_size, seed, steps, cfg, sampler_name, scheduler,
                     positive, negative, denoise, feather, noise_mask, force_inpaint,
                     bbox_threshold, bbox_dilation, bbox_crop_factor,
                     sam_detection_hint, sam_dilation, sam_threshold, sam_bbox_expansion, sam_mask_hint_threshold,
                     sam_mask_hint_use_negative, drop_size,
                     bbox_detector, segm_detector=None, sam_model_opt=None, wildcard_opt=None, detailer_hook=None,
                     refiner_ratio=None, refiner_model=None, refiner_clip=None, refiner_positive=None, refiner_negative=None):

        # make default prompt as 'face' if empty prompt for CLIPSeg
        bbox_detector.setAux('face')
        segs = bbox_detector.detect(image, bbox_threshold, bbox_dilation, bbox_crop_factor, drop_size)
        bbox_detector.setAux(None)

        # bbox + sam combination
        if sam_model_opt is not None:
            sam_mask = core.make_sam_mask(sam_model_opt, segs, image, sam_detection_hint, sam_dilation,
                                          sam_threshold, sam_bbox_expansion, sam_mask_hint_threshold,
                                          sam_mask_hint_use_negative, )
            segs = core.segs_bitwise_and_mask(segs, sam_mask)

        elif segm_detector is not None:
            segm_segs = segm_detector.detect(image, bbox_threshold, bbox_dilation, bbox_crop_factor, drop_size)
            segm_mask = core.segs_to_combined_mask(segm_segs)
            segs = core.segs_bitwise_and_mask(segs, segm_mask)

        enhanced_img, _, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list = \
            DetailerForEach.do_detail(image, segs, model, clip, vae, guide_size, guide_size_for_bbox, max_size, seed, steps, cfg,
                                      sampler_name, scheduler, positive, negative, denoise, feather, noise_mask,
                                      force_inpaint, wildcard_opt, detailer_hook,
                                      refiner_ratio=refiner_ratio, refiner_model=refiner_model,
                                      refiner_clip=refiner_clip, refiner_positive=refiner_positive,
                                      refiner_negative=refiner_negative)

        # Mask Generator
        mask = core.segs_to_combined_mask(segs)

        if len(cropped_enhanced) == 0:
            cropped_enhanced = [empty_pil_tensor()]

        if len(cropped_enhanced_alpha) == 0:
            cropped_enhanced_alpha = [empty_pil_tensor()]

        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return enhanced_img, cropped_enhanced, cropped_enhanced_alpha, mask, cnet_pil_list

    def doit(self, image, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
             positive, negative, denoise, feather, noise_mask, force_inpaint,
             bbox_threshold, bbox_dilation, bbox_crop_factor,
             sam_detection_hint, sam_dilation, sam_threshold, sam_bbox_expansion, sam_mask_hint_threshold,
             sam_mask_hint_use_negative, drop_size, bbox_detector, wildcard, sam_model_opt=None, segm_detector_opt=None, detailer_hook=None):

        enhanced_img, cropped_enhanced, cropped_enhanced_alpha, mask, cnet_pil_list = FaceDetailer.enhance_face(
            image, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
            positive, negative, denoise, feather, noise_mask, force_inpaint,
            bbox_threshold, bbox_dilation, bbox_crop_factor,
            sam_detection_hint, sam_dilation, sam_threshold, sam_bbox_expansion, sam_mask_hint_threshold,
            sam_mask_hint_use_negative, drop_size, bbox_detector, segm_detector_opt, sam_model_opt, wildcard, detailer_hook)

        pipe = (model, clip, vae, positive, negative, wildcard, bbox_detector, segm_detector_opt, sam_model_opt, detailer_hook, None, None, None, None)
        return enhanced_img, cropped_enhanced, cropped_enhanced_alpha, mask, pipe, cnet_pil_list


class LatentPixelScale:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "samples": ("LATENT", ),
                     "scale_method": (s.upscale_methods,),
                     "scale_factor": ("FLOAT", {"default": 1.5, "min": 0.1, "max": 10000, "step": 0.1}),
                     "vae": ("VAE", ),
                     "use_tiled_vae": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                    },
                "optional": {
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                    }
                }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, samples, scale_method, scale_factor, vae, use_tiled_vae, upscale_model_opt=None):
        if upscale_model_opt is None:
            latent = core.latent_upscale_on_pixel_space(samples, scale_method, scale_factor, vae, use_tile=use_tiled_vae)
        else:
            latent = core.latent_upscale_on_pixel_space_with_model(samples, scale_method, upscale_model_opt, scale_factor, vae, use_tile=use_tiled_vae)
        return (latent,)


class NoiseInjectionDetailerHookProvider:
    schedules = ["simple"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "source": (["CPU", "GPU"],),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 200.0, "step": 0.01}),
                    },
                }

    RETURN_TYPES = ("DETAILER_HOOK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    def doit(self, source, seed, strength):
        try:
            hook = core.InjectNoiseHook(source, seed, strength, strength)
            hook.set_steps((1, 1))
            return (hook, )
        except Exception as e:
            print("[ERROR] NoiseInjectionDetailerHookProvider: 'ComfyUI Noise' custom node isn't installed. You must install 'BlenderNeko/ComfyUI Noise' extension to use this node.")
            print(f"\t{e}")
            pass


class CfgScheduleHookProvider:
    schedules = ["simple"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "schedule_for_iteration": (s.schedules,),
                     "target_cfg": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 100.0}),
                    },
                }

    RETURN_TYPES = ("PK_HOOK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, schedule_for_iteration, target_cfg):
        hook = None
        if schedule_for_iteration == "simple":
            hook = core.SimpleCfgScheduleHook(target_cfg)

        return (hook, )


class NoiseInjectionHookProvider:
    schedules = ["simple"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "schedule_for_iteration": (s.schedules,),
                     "source": (["CPU", "GPU"],),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "start_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 200.0, "step": 0.01}),
                     "end_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 200.0, "step": 0.01}),
                    },
                }

    RETURN_TYPES = ("PK_HOOK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, schedule_for_iteration, source, seed, start_strength, end_strength):
        try:
            hook = None
            if schedule_for_iteration == "simple":
                hook = core.InjectNoiseHook(source, seed, start_strength, end_strength)

            return (hook, )
        except Exception as e:
            print("[ERROR] NoiseInjectionHookProvider: 'ComfyUI Noise' custom node isn't installed. You must install 'BlenderNeko/ComfyUI Noise' extension to use this node.")
            print(f"\t{e}")
            pass


class DenoiseScheduleHookProvider:
    schedules = ["simple"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "schedule_for_iteration": (s.schedules,),
                     "target_denoise": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 100.0}),
                    },
                }

    RETURN_TYPES = ("PK_HOOK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, schedule_for_iteration, target_denoise):
        hook = None
        if schedule_for_iteration == "simple":
            hook = core.SimpleDenoiseScheduleHook(target_denoise)

        return (hook, )


class PixelKSampleHookCombine:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "hook1": ("PK_HOOK",),
                     "hook2": ("PK_HOOK",),
                    },
                }

    RETURN_TYPES = ("PK_HOOK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, hook1, hook2):
        hook = core.PixelKSampleHookCombine(hook1, hook2)
        return (hook, )


class PixelTiledKSampleUpscalerProvider:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "scale_method": (s.upscale_methods,),
                    "model": ("MODEL",),
                    "vae": ("VAE",),
                    "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                    "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                    "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                    "tile_width": ("INT", {"default": 512, "min": 320, "max": MAX_RESOLUTION, "step": 64}),
                    "tile_height": ("INT", {"default": 512, "min": 320, "max": MAX_RESOLUTION, "step": 64}),
                    "tiling_strategy": (["random", "padded", 'simple'], ),
                    },
                "optional": {
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler, positive, negative, denoise, tile_width, tile_height, tiling_strategy, upscale_model_opt=None, pk_hook_opt=None):
        if "BNK_TiledKSampler" in nodes.NODE_CLASS_MAPPINGS:
            upscaler = core.PixelTiledKSampleUpscaler(scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler, positive, negative, denoise, tile_width, tile_height, tiling_strategy, upscale_model_opt, pk_hook_opt, tile_size=max(tile_width, tile_height))
            return (upscaler, )
        else:
            print("[ERROR] PixelTiledKSampleUpscalerProvider: ComfyUI_TiledKSampler custom node isn't installed. You must install BlenderNeko/ComfyUI_TiledKSampler extension to use this node.")


class PixelTiledKSampleUpscalerProviderPipe:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "scale_method": (s.upscale_methods,),
                    "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                    "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                    "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                    "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                    "tile_width": ("INT", {"default": 512, "min": 320, "max": MAX_RESOLUTION, "step": 64}),
                    "tile_height": ("INT", {"default": 512, "min": 320, "max": MAX_RESOLUTION, "step": 64}),
                    "tiling_strategy": (["random", "padded", 'simple'], ),
                    "basic_pipe": ("BASIC_PIPE",)
                    },
                "optional": {
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, scale_method, seed, steps, cfg, sampler_name, scheduler, denoise, tile_width, tile_height, tiling_strategy, basic_pipe, upscale_model_opt=None, pk_hook_opt=None):
        if "BNK_TiledKSampler" in nodes.NODE_CLASS_MAPPINGS:
            model, _, vae, positive, negative = basic_pipe
            upscaler = core.PixelTiledKSampleUpscaler(scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler, positive, negative, denoise, tile_width, tile_height, tiling_strategy, upscale_model_opt, pk_hook_opt, tile_size=max(tile_width, tile_height))
            return (upscaler, )
        else:
            print("[ERROR] PixelTiledKSampleUpscalerProviderPipe: ComfyUI_TiledKSampler custom node isn't installed. You must install BlenderNeko/ComfyUI_TiledKSampler extension to use this node.")


class PixelKSampleUpscalerProvider:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "scale_method": (s.upscale_methods,),
                    "model": ("MODEL",),
                    "vae": ("VAE",),
                    "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                    "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                    "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                    "use_tiled_vae": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                    "tile_size": ("INT", {"default": 512, "min": 320, "max": 4096, "step": 64}),
                    },
                "optional": {
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler, positive, negative, denoise,
             use_tiled_vae, upscale_model_opt=None, pk_hook_opt=None, tile_size=512):
        upscaler = core.PixelKSampleUpscaler(scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler,
                                             positive, negative, denoise, use_tiled_vae, upscale_model_opt, pk_hook_opt,
                                             tile_size=tile_size)
        return (upscaler, )


class PixelKSampleUpscalerProviderPipe(PixelKSampleUpscalerProvider):
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "scale_method": (s.upscale_methods,),
                    "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                    "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                    "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                    "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                    "use_tiled_vae": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                    "basic_pipe": ("BASIC_PIPE",),
                    "tile_size": ("INT", {"default": 512, "min": 320, "max": 4096, "step": 64}),
                    },
                "optional": {
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER",)
    FUNCTION = "doit_pipe"

    CATEGORY = "ImpactPack/Upscale"

    def doit_pipe(self, scale_method, seed, steps, cfg, sampler_name, scheduler, denoise,
                  use_tiled_vae, basic_pipe, upscale_model_opt=None, pk_hook_opt=None, tile_size=512):
        model, _, vae, positive, negative = basic_pipe
        upscaler = core.PixelKSampleUpscaler(scale_method, model, vae, seed, steps, cfg, sampler_name, scheduler,
                                             positive, negative, denoise, use_tiled_vae, upscale_model_opt, pk_hook_opt,
                                             tile_size=tile_size)
        return (upscaler, )


class TwoSamplersForMaskUpscalerProvider:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "scale_method": (s.upscale_methods,),
                     "full_sample_schedule": (
                         ["none", "interleave1", "interleave2", "interleave3",
                          "last1", "last2",
                          "interleave1+last1", "interleave2+last1", "interleave3+last1",
                          ],),
                     "use_tiled_vae": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                     "base_sampler": ("KSAMPLER", ),
                     "mask_sampler": ("KSAMPLER", ),
                     "mask": ("MASK", ),
                     "vae": ("VAE",),
                     "tile_size": ("INT", {"default": 512, "min": 320, "max": 4096, "step": 64}),
                     },
                "optional": {
                        "full_sampler_opt": ("KSAMPLER",),
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_base_opt": ("PK_HOOK", ),
                        "pk_hook_mask_opt": ("PK_HOOK", ),
                        "pk_hook_full_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, scale_method, full_sample_schedule, use_tiled_vae, base_sampler, mask_sampler, mask, vae,
             full_sampler_opt=None, upscale_model_opt=None,
             pk_hook_base_opt=None, pk_hook_mask_opt=None, pk_hook_full_opt=None, tile_size=512):
        upscaler = core.TwoSamplersForMaskUpscaler(scale_method, full_sample_schedule, use_tiled_vae,
                                                   base_sampler, mask_sampler, mask, vae, full_sampler_opt, upscale_model_opt,
                                                   pk_hook_base_opt, pk_hook_mask_opt, pk_hook_full_opt, tile_size=tile_size)
        return (upscaler, )


class TwoSamplersForMaskUpscalerProviderPipe:
    upscale_methods = ["nearest-exact", "bilinear", "area"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "scale_method": (s.upscale_methods,),
                     "full_sample_schedule": (
                         ["none", "interleave1", "interleave2", "interleave3",
                          "last1", "last2",
                          "interleave1+last1", "interleave2+last1", "interleave3+last1",
                          ],),
                     "use_tiled_vae": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                     "base_sampler": ("KSAMPLER", ),
                     "mask_sampler": ("KSAMPLER", ),
                     "mask": ("MASK", ),
                     "basic_pipe": ("BASIC_PIPE",),
                     "tile_size": ("INT", {"default": 512, "min": 320, "max": 4096, "step": 64}),
                     },
                "optional": {
                        "full_sampler_opt": ("KSAMPLER",),
                        "upscale_model_opt": ("UPSCALE_MODEL", ),
                        "pk_hook_base_opt": ("PK_HOOK", ),
                        "pk_hook_mask_opt": ("PK_HOOK", ),
                        "pk_hook_full_opt": ("PK_HOOK", ),
                    }
                }

    RETURN_TYPES = ("UPSCALER", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, scale_method, full_sample_schedule, use_tiled_vae, base_sampler, mask_sampler, mask, basic_pipe,
             full_sampler_opt=None, upscale_model_opt=None,
             pk_hook_base_opt=None, pk_hook_mask_opt=None, pk_hook_full_opt=None, tile_size=512):
        _, _, vae, _, _ = basic_pipe
        upscaler = core.TwoSamplersForMaskUpscaler(scale_method, full_sample_schedule, use_tiled_vae,
                                                   base_sampler, mask_sampler, mask, vae, full_sampler_opt, upscale_model_opt,
                                                   pk_hook_base_opt, pk_hook_mask_opt, pk_hook_full_opt, tile_size=tile_size)
        return (upscaler, )


class IterativeLatentUpscale:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "samples": ("LATENT", ),
                     "upscale_factor": ("FLOAT", {"default": 1.5, "min": 1, "max": 10000, "step": 0.1}),
                     "steps": ("INT", {"default": 3, "min": 1, "max": 10000, "step": 1}),
                     "temp_prefix": ("STRING", {"default": ""}),
                     "upscaler": ("UPSCALER",)
                },
                "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, samples, upscale_factor, steps, temp_prefix, upscaler, unique_id):
        w = samples['samples'].shape[3]*8  # image width
        h = samples['samples'].shape[2]*8  # image height

        if temp_prefix == "":
            temp_prefix = None

        upscale_factor_unit = max(0, (upscale_factor-1.0)/steps)
        current_latent = samples
        scale = 1

        for i in range(steps-1):
            scale += upscale_factor_unit
            new_w = w*scale
            new_h = h*scale
            core.update_node_status(unique_id, f"{i+1}/{steps} steps | x{scale:.2f}", (i+1)/steps)
            print(f"IterativeLatentUpscale[{i+1}/{steps}]: {new_w:.1f}x{new_h:.1f} (scale:{scale:.2f}) ")
            step_info = i, steps
            current_latent = upscaler.upscale_shape(step_info, current_latent, new_w, new_h, temp_prefix)

        if scale < upscale_factor:
            new_w = w*upscale_factor
            new_h = h*upscale_factor
            core.update_node_status(unique_id, f"Final step | x{upscale_factor:.2f}", 1.0)
            print(f"IterativeLatentUpscale[Final]: {new_w:.1f}x{new_h:.1f} (scale:{upscale_factor:.2f}) ")
            step_info = steps, steps
            current_latent = upscaler.upscale_shape(step_info, current_latent, new_w, new_h, temp_prefix)

        core.update_node_status(unique_id, "", None)

        return (current_latent, )


class IterativeImageUpscale:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "pixels": ("IMAGE", ),
                     "upscale_factor": ("FLOAT", {"default": 1.5, "min": 1, "max": 10000, "step": 0.1}),
                     "steps": ("INT", {"default": 3, "min": 1, "max": 10000, "step": 1}),
                     "temp_prefix": ("STRING", {"default": ""}),
                     "upscaler": ("UPSCALER",),
                     "vae": ("VAE",),
                },
                "hidden": {"unique_id": "UNIQUE_ID"}
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Upscale"

    def doit(self, pixels, upscale_factor, steps, temp_prefix, upscaler, vae, unique_id):
        if temp_prefix == "":
            temp_prefix = None

        core.update_node_status(unique_id, "VAEEncode (first)", 0)
        if upscaler.is_tiled:
            latent = nodes.VAEEncodeTiled().encode(vae, pixels, upscaler.tile_size)[0]
        else:
            latent = nodes.VAEEncode().encode(vae, pixels)[0]

        refined_latent = IterativeLatentUpscale().doit(latent, upscale_factor, steps, temp_prefix, upscaler, unique_id)

        core.update_node_status(unique_id, "VAEDecode (final)", 1.0)
        if upscaler.is_tiled:
            pixels = nodes.VAEDecodeTiled().decode(vae, refined_latent[0], upscaler.tile_size)[0]
        else:
            pixels = nodes.VAEDecode().decode(vae, refined_latent[0])[0]

        core.update_node_status(unique_id, "", None)

        return (pixels, )


class FaceDetailerPipe:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "detailer_pipe": ("DETAILER_PIPE",),
                     "guide_size": ("FLOAT", {"default": 256, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "guide_size_for": ("BOOLEAN", {"default": True, "label_on": "bbox", "label_off": "crop_region"}),
                     "max_size": ("FLOAT", {"default": 768, "min": 64, "max": nodes.MAX_RESOLUTION, "step": 8}),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                     "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                     "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                     "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                     "denoise": ("FLOAT", {"default": 0.5, "min": 0.0001, "max": 1.0, "step": 0.01}),
                     "feather": ("INT", {"default": 5, "min": 0, "max": 100, "step": 1}),
                     "noise_mask": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "force_inpaint": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),

                     "bbox_threshold": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "bbox_dilation": ("INT", {"default": 10, "min": 0, "max": 255, "step": 1}),
                     "bbox_crop_factor": ("FLOAT", {"default": 3.0, "min": 1.0, "max": 10, "step": 0.1}),

                     "sam_detection_hint": (["center-1", "horizontal-2", "vertical-2", "rect-4", "diamond-4", "mask-area", "mask-points", "mask-point-bbox", "none"],),
                     "sam_dilation": ("INT", {"default": 0, "min": 0, "max": 255, "step": 1}),
                     "sam_threshold": ("FLOAT", {"default": 0.93, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "sam_bbox_expansion": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                     "sam_mask_hint_threshold": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                     "sam_mask_hint_use_negative": (["False", "Small", "Outter"],),

                     "drop_size": ("INT", {"min": 1, "max": MAX_RESOLUTION, "step": 1, "default": 10}),
                     "refiner_ratio": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0}),
                     },
                }

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "MASK", "DETAILER_PIPE", "IMAGE")
    RETURN_NAMES = ("image", "cropped_refined", "cropped_enhanced_alpha", "mask", "detailer_pipe", "cnet_images")
    OUTPUT_IS_LIST = (False, True, True, False, False, True)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Simple"

    def doit(self, image, detailer_pipe, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
             denoise, feather, noise_mask, force_inpaint, bbox_threshold, bbox_dilation, bbox_crop_factor,
             sam_detection_hint, sam_dilation, sam_threshold, sam_bbox_expansion,
             sam_mask_hint_threshold, sam_mask_hint_use_negative, drop_size, refiner_ratio=None):

        model, clip, vae, positive, negative, wildcard, bbox_detector, segm_detector, sam_model_opt, detailer_hook, \
            refiner_model, refiner_clip, refiner_positive, refiner_negative = detailer_pipe

        enhanced_img, cropped_enhanced, cropped_enhanced_alpha, mask, cnet_pil_list = FaceDetailer.enhance_face(
            image, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
            positive, negative, denoise, feather, noise_mask, force_inpaint,
            bbox_threshold, bbox_dilation, bbox_crop_factor,
            sam_detection_hint, sam_dilation, sam_threshold, sam_bbox_expansion, sam_mask_hint_threshold,
            sam_mask_hint_use_negative, drop_size, bbox_detector, segm_detector, sam_model_opt, wildcard, detailer_hook,
            refiner_ratio=refiner_ratio, refiner_model=refiner_model,
            refiner_clip=refiner_clip, refiner_positive=refiner_positive, refiner_negative=refiner_negative)

        if len(cropped_enhanced) == 0:
            cropped_enhanced = [empty_pil_tensor()]

        if len(cropped_enhanced_alpha) == 0:
            cropped_enhanced_alpha = [empty_pil_tensor()]

        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return enhanced_img, cropped_enhanced, cropped_enhanced_alpha, mask, detailer_pipe, cnet_pil_list


class DetailerForEachTest(DetailerForEach):
    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = ("image", "cropped", "cropped_refined", "cropped_refined_alpha", "cnet_images")
    OUTPUT_IS_LIST = (False, True, True, True, True)

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    def doit(self, image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name,
             scheduler, positive, negative, denoise, feather, noise_mask, force_inpaint, wildcard, detailer_hook=None):

        enhanced_img, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list = \
            DetailerForEach.do_detail(image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps,
                                      cfg, sampler_name, scheduler, positive, negative, denoise, feather, noise_mask,
                                      force_inpaint, wildcard, detailer_hook)

        # set fallback image
        if len(cropped) == 0:
            cropped = [empty_pil_tensor()]

        if len(cropped_enhanced) == 0:
            cropped_enhanced = [empty_pil_tensor()]

        if len(cropped_enhanced_alpha) == 0:
            cropped_enhanced_alpha = [empty_pil_tensor()]

        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return enhanced_img, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list


class DetailerForEachTestPipe(DetailerForEachPipe):
    RETURN_TYPES = ("IMAGE", "SEGS", "BASIC_PIPE", "IMAGE", "IMAGE", "IMAGE", "IMAGE", )
    RETURN_NAMES = ("image", "segs", "basic_pipe", "cropped", "cropped_refined", "cropped_refined_alpha", 'cnet_images')
    OUTPUT_IS_LIST = (False, False, False, True, True, True, True)

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    def doit(self, image, segs, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
             denoise, feather, noise_mask, force_inpaint, basic_pipe, wildcard, refiner_ratio=None, detailer_hook=None, refiner_basic_pipe_opt=None):

        model, clip, vae, positive, negative = basic_pipe

        if refiner_basic_pipe_opt is None:
            refiner_model, refiner_clip, refiner_positive, refiner_negative = None, None, None, None
        else:
            refiner_model, refiner_clip, _, refiner_positive, refiner_negative = refiner_basic_pipe_opt

        enhanced_img, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list = \
            DetailerForEach.do_detail(image, segs, model, clip, vae, guide_size, guide_size_for, max_size, seed, steps, cfg,
                                      sampler_name, scheduler, positive, negative, denoise, feather, noise_mask,
                                      force_inpaint, wildcard, detailer_hook,
                                      refiner_ratio=refiner_ratio, refiner_model=refiner_model,
                                      refiner_clip=refiner_clip, refiner_positive=refiner_positive,
                                      refiner_negative=refiner_negative)

        # set fallback image
        if len(cropped) == 0:
            cropped = [empty_pil_tensor()]

        if len(cropped_enhanced) == 0:
            cropped_enhanced = [empty_pil_tensor()]

        if len(cropped_enhanced_alpha) == 0:
            cropped_enhanced_alpha = [empty_pil_tensor()]

        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return enhanced_img, segs, basic_pipe, cropped, cropped_enhanced, cropped_enhanced_alpha, cnet_pil_list


class SegsBitwiseAndMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "segs": ("SEGS",),
                        "mask": ("MASK",),
                    }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, segs, mask):
        return (core.segs_bitwise_and_mask(segs, mask), )
    

class SegsBitwiseAndMaskForEach:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "segs": ("SEGS",),
                        "masks": ("MASKS",),
                    }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, segs, masks):
        return (core.apply_mask_to_each_seg(segs, masks), )


class BitwiseAndMaskForEach:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
            {
                "base_segs": ("SEGS",),
                "mask_segs": ("SEGS",),
            }
        }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, base_segs, mask_segs):

        result = []

        for bseg in base_segs[1]:
            cropped_mask1 = bseg.cropped_mask.copy()
            crop_region1 = bseg.crop_region

            for mseg in mask_segs[1]:
                cropped_mask2 = mseg.cropped_mask
                crop_region2 = mseg.crop_region

                # compute the intersection of the two crop regions
                intersect_region = (max(crop_region1[0], crop_region2[0]),
                                    max(crop_region1[1], crop_region2[1]),
                                    min(crop_region1[2], crop_region2[2]),
                                    min(crop_region1[3], crop_region2[3]))

                overlapped = False

                # set all pixels in cropped_mask1 to 0 except for those that overlap with cropped_mask2
                for i in range(intersect_region[0], intersect_region[2]):
                    for j in range(intersect_region[1], intersect_region[3]):
                        if cropped_mask1[j - crop_region1[1], i - crop_region1[0]] == 1 and \
                                cropped_mask2[j - crop_region2[1], i - crop_region2[0]] == 1:
                            # pixel overlaps with both masks, keep it as 1
                            overlapped = True
                            pass
                        else:
                            # pixel does not overlap with both masks, set it to 0
                            cropped_mask1[j - crop_region1[1], i - crop_region1[0]] = 0

                if overlapped:
                    item = SEG(bseg.cropped_image, cropped_mask1, bseg.confidence, bseg.crop_region, bseg.bbox, bseg.label, None)
                    result.append(item)

        return ((base_segs[0], result),)


class SubtractMaskForEach:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "base_segs": ("SEGS",),
                        "mask_segs": ("SEGS",),
                    }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, base_segs, mask_segs):

        result = []

        for bseg in base_segs[1]:
            cropped_mask1 = bseg.cropped_mask.copy()
            crop_region1 = bseg.crop_region

            for mseg in mask_segs[1]:
                cropped_mask2 = mseg.cropped_mask
                crop_region2 = mseg.crop_region

                # compute the intersection of the two crop regions
                intersect_region = (max(crop_region1[0], crop_region2[0]),
                                    max(crop_region1[1], crop_region2[1]),
                                    min(crop_region1[2], crop_region2[2]),
                                    min(crop_region1[3], crop_region2[3]))

                changed = False

                # subtract operation
                for i in range(intersect_region[0], intersect_region[2]):
                    for j in range(intersect_region[1], intersect_region[3]):
                        if cropped_mask1[j - crop_region1[1], i - crop_region1[0]] == 1 and \
                                cropped_mask2[j - crop_region2[1], i - crop_region2[0]] == 1:
                            # pixel overlaps with both masks, set it as 0
                            changed = True
                            cropped_mask1[j - crop_region1[1], i - crop_region1[0]] = 0
                        else:
                            # pixel does not overlap with both masks, don't care
                            pass

                if changed:
                    item = SEG(bseg.cropped_image, cropped_mask1, bseg.confidence, bseg.crop_region, bseg.bbox, bseg.label, None)
                    result.append(item)
                else:
                    result.append(base_segs)

        return ((base_segs[0], result),)


class MasksToMaskList:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "masks": ("MASKS", ),
                      }
                }

    RETURN_TYPES = ("MASK", )
    OUTPUT_IS_LIST = (True, )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, masks):
        if masks is None:
            empty_mask = torch.zeros((64,64), dtype=torch.float32, device="cpu")
            return ([empty_mask], )

        res = []

        for mask in masks:
            res.append(mask)

        print(f"mask len: {len(res)}")

        return (res, )


class MaskListToMaskBatch:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "mask": ("MASK", ),
                      }
                }

    INPUT_IS_LIST = True

    RETURN_TYPES = ("MASKS", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask):
        mask_batch = torch.stack(mask, dim=0)
        return (mask_batch, )


class ToBinaryMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                      "mask": ("MASK",),
                      "threshold": ("INT", {"default": 20, "min": 1, "max": 255}),
                    }
                }

    RETURN_TYPES = ("MASK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask, threshold):
        mask = to_binary_mask(mask, threshold/255.0)
        return (mask,)


class BitwiseAndMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "mask1": ("MASK",),
                        "mask2": ("MASK",),
                    }
                }

    RETURN_TYPES = ("MASK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask1, mask2):
        mask = bitwise_and_masks(mask1, mask2)
        return (mask,)


class SubtractMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "mask1": ("MASK", ),
                        "mask2": ("MASK", ),
                      }
                }
    
    RETURN_TYPES = ("MASK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask1, mask2):
        mask = subtract_masks(mask1, mask2)
        return (mask,)


class AddMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "mask1": ("MASK",),
            "mask2": ("MASK",),
        }
        }

    RETURN_TYPES = ("MASK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask1, mask2):
        mask = add_masks(mask1, mask2)
        return (mask,)


import nodes


def get_image_hash(arr):
    split_index1 = arr.shape[0] // 2
    split_index2 = arr.shape[1] // 2
    part1 = arr[:split_index1, :split_index2]
    part2 = arr[:split_index1, split_index2:]
    part3 = arr[split_index1:, :split_index2]
    part4 = arr[split_index1:, split_index2:]

    # 각 부분을 합산
    sum1 = np.sum(part1)
    sum2 = np.sum(part2)
    sum3 = np.sum(part3)
    sum4 = np.sum(part4)

    return hash((sum1, sum2, sum3, sum4))


class PreviewBridge(nodes.PreviewImage):
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"images": ("IMAGE",), },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
                "optional": {"image": (["#placeholder"], )},
                }

    RETURN_TYPES = ("IMAGE", "MASK", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def __init__(self):
        super().__init__()
        self.prev_hash = None

    def doit(self, images, image, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None, unique_id=None):
        if image != "#placeholder" and isinstance(image, str):
            image_path = folder_paths.get_annotated_filepath(image)
            img = Image.open(image_path).convert("RGB")
            data = np.array(img)
            image_hash = get_image_hash(data)
        else:
            data = (255. * images[0].cpu().numpy()).astype(int)
            image_hash = get_image_hash(data)

        is_changed = False
        if self.prev_hash is None or self.prev_hash != image_hash:
            self.prev_hash = image_hash
            is_changed = True

        if is_changed or image == "#placeholder":
            # new input image
            res = self.save_images(images, filename_prefix, prompt, extra_pnginfo)

            item = res['ui']['images'][0]

            if not item['filename'].endswith(']'):
                filepath = f"{item['filename']} [{item['type']}]"
            else:
                filepath = item['filename']

            image, mask = nodes.LoadImage().load_image(filepath)

            res['ui']['aux'] = [image_hash, res['ui']['images']]
            res['result'] = (image, mask, )

            return res

        else:
            # new mask
            if '0' in image:  # fallback
                image = image['0']

            forward = {'filename': image['forward_filename'],
                       'subfolder': image['forward_subfolder'],
                       'type': image['forward_type'], }

            res = {'ui': {'images': [forward]}}

            imgpath = ""
            if 'subfolder' in image and image['subfolder'] != "":
                imgpath = image['subfolder'] + "/"

            imgpath += f"{image['filename']}"

            if 'type' in image and image['type'] != "":
                imgpath += f" [{image['type']}]"

            res['ui']['aux'] = [image_hash, [forward]]
            res['result'] = nodes.LoadImage().load_image(imgpath)

            return res


class ImageReceiver(nodes.LoadImage):
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
        return {"required": {
                    "image": (sorted(files), ),
                    "link_id": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}), },
                }

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, image, link_id):
        return nodes.LoadImage().load_image(image)

    @classmethod
    def VALIDATE_INPUTS(s, image, link_id):
        if not folder_paths.exists_annotated_filepath(image) or image.startswith("/") or ".." in image:
            return "Invalid image file: {}".format(image)

        return True


from server import PromptServer

class ImageSender(nodes.PreviewImage):
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "images": ("IMAGE", ),
                    "filename_prefix": ("STRING", {"default": "ImgSender"}),
                    "link_id": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}), },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    OUTPUT_NODE = True

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, images, filename_prefix="ImgSender", link_id=0, prompt=None, extra_pnginfo=None):
        result = nodes.PreviewImage().save_images(images, filename_prefix, prompt, extra_pnginfo)
        PromptServer.instance.send_sync("img-send", {"link_id": link_id, "images": result['ui']['images']})
        return result


class LatentReceiver:
    def __init__(self):
        self.input_dir = folder_paths.get_input_directory()
        self.type = "input"

    @classmethod
    def INPUT_TYPES(s):
        def check_file_extension(x):
            return x.endswith(".latent") or x.endswith(".latent.png")

        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f)) and check_file_extension(f)]
        return {"required": {
                    "latent": (sorted(files), ),
                    "link_id": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}),
                    },
                }

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    RETURN_TYPES = ("LATENT",)

    @staticmethod
    def load_preview_latent(image_path):
        if not os.path.exists(image_path):
            return None

        image = Image.open(image_path)
        exif_data = piexif.load(image.info["exif"])

        if piexif.ExifIFD.UserComment in exif_data["Exif"]:
            compressed_data = exif_data["Exif"][piexif.ExifIFD.UserComment]
            compressed_data_io = BytesIO(compressed_data)
            with zipfile.ZipFile(compressed_data_io, mode='r') as archive:
                tensor_bytes = archive.read("latent")
            tensor = safetensors.torch.load(tensor_bytes)
            return {"samples": tensor['latent_tensor']}
        return None

    def parse_filename(self, filename):
        pattern = r"^(.*)/(.*?)\[(.*)\]\s*$"
        match = re.match(pattern, filename)
        if match:
            subfolder = match.group(1)
            filename = match.group(2).rstrip()
            file_type = match.group(3)
        else:
            subfolder = ''
            file_type = self.type

        return {'filename': filename, 'subfolder': subfolder, 'type': file_type}

    def doit(self, **kwargs):
        if 'latent' not in kwargs:
            return (torch.zeros([1, 4, 8, 8]), )

        latent = kwargs['latent']

        latent_name = latent
        latent_path = folder_paths.get_annotated_filepath(latent_name)

        if latent.endswith(".latent"):
            latent = safetensors.torch.load_file(latent_path, device="cpu")
            multiplier = 1.0
            if "latent_format_version_0" not in latent:
                multiplier = 1.0 / 0.18215
            samples = {"samples": latent["latent_tensor"].float() * multiplier}
        else:
            samples = LatentReceiver.load_preview_latent(latent_path)

        if samples is None:
            samples = {'samples': torch.zeros([1, 4, 8, 8])}

        preview = self.parse_filename(latent_name)

        return {
                'ui': {"images": [preview]},
                'result': (samples, )
                }

    @classmethod
    def IS_CHANGED(s, latent, link_id):
        image_path = folder_paths.get_annotated_filepath(latent)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(s, latent, link_id):
        if not folder_paths.exists_annotated_filepath(latent) or latent.startswith("/") or ".." in latent:
            return "Invalid latent file: {}".format(latent)
        return True


class LatentSender(nodes.SaveLatent):
    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                             "samples": ("LATENT", ),
                             "filename_prefix": ("STRING", {"default": "latents/LatentSender"}),
                             "link_id": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}),
                             "preview_method": (["Latent2RGB-SDXL", "Latent2RGB-SD15", "TAESDXL", "TAESD15"],)
                             },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    OUTPUT_NODE = True

    RETURN_TYPES = ()

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    @staticmethod
    def save_to_file(tensor_bytes, prompt, extra_pnginfo, image, image_path):
        compressed_data = BytesIO()
        with zipfile.ZipFile(compressed_data, mode='w') as archive:
            archive.writestr("latent", tensor_bytes)
        image = image.copy()
        exif_data = {"Exif": {piexif.ExifIFD.UserComment: compressed_data.getvalue()}}

        metadata = PngInfo()
        if prompt is not None:
            metadata.add_text("prompt", json.dumps(prompt))
        if extra_pnginfo is not None:
            for x in extra_pnginfo:
                metadata.add_text(x, json.dumps(extra_pnginfo[x]))

        exif_bytes = piexif.dump(exif_data)
        image.save(image_path, format='png', exif=exif_bytes, pnginfo=metadata, optimize=True)

    @staticmethod
    def prepare_preview(latent_tensor, preview_method):
        from comfy.cli_args import LatentPreviewMethod
        import comfy.latent_formats as latent_formats

        lower_bound = 128
        upper_bound = 256

        if preview_method == "Latent2RGB-SD15":
            latent_format = latent_formats.SD15()
            method = LatentPreviewMethod.Latent2RGB
        elif preview_method == "TAESD15":
            latent_format = latent_formats.SD15()
            method = LatentPreviewMethod.TAESD
        elif preview_method == "TAESDXL":
            latent_format = latent_formats.SDXL()
            method = LatentPreviewMethod.TAESD
        else:  # preview_method == "Latent2RGB-SDXL"
            latent_format = latent_formats.SDXL()
            method = LatentPreviewMethod.Latent2RGB

        previewer = core.get_previewer("cpu", latent_format=latent_format, force=True, method=method)

        image = previewer.decode_latent_to_preview(latent_tensor)
        min_size = min(image.size[0], image.size[1])
        max_size = max(image.size[0], image.size[1])

        scale_factor = 1
        if max_size > upper_bound:
            scale_factor = upper_bound/max_size

        # prevent too small preview
        if min_size*scale_factor < lower_bound:
            scale_factor = lower_bound/min_size

        w = int(image.size[0] * scale_factor)
        h = int(image.size[1] * scale_factor)

        image = image.resize((w, h), resample=Image.NEAREST)

        return LatentSender.attach_format_text(image)

    @staticmethod
    def attach_format_text(image):
        width_a, height_a = image.size

        letter_image = Image.open(latent_letter_path)
        width_b, height_b = letter_image.size

        new_width = max(width_a, width_b)
        new_height = height_a + height_b

        new_image = Image.new('RGB', (new_width, new_height), (0, 0, 0))

        offset_x = (new_width - width_b) // 2
        offset_y = (height_a + (new_height - height_a - height_b) // 2)
        new_image.paste(letter_image, (offset_x, offset_y))

        new_image.paste(image, (0, 0))

        return new_image

    def doit(self, samples, filename_prefix="latents/LatentSender", link_id=0, preview_method="Latent2RGB-SDXL", prompt=None, extra_pnginfo=None):
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir)

        # load preview
        preview = LatentSender.prepare_preview(samples['samples'], preview_method)

        # support save metadata for latent sharing
        file = f"{filename}_{counter:05}_.latent.png"
        fullpath = os.path.join(full_output_folder, file)

        output = {"latent_tensor": samples["samples"]}

        tensor_bytes = safetensors.torch.save(output)
        LatentSender.save_to_file(tensor_bytes, prompt, extra_pnginfo, preview, fullpath)

        latent_path = {
                    'filename': file,
                    'subfolder': subfolder,
                    'type': self.type
                    }

        PromptServer.instance.send_sync("latent-send", {"link_id": link_id, "images": [latent_path]})

        return {'ui': {'images': [latent_path]}}


class ImageMaskSwitch:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "select": ("INT", {"default": 1, "min": 1, "max": 4, "step": 1}),
                    "images1": ("IMAGE", ),
                    },
            
                "optional": {
                    "mask1_opt": ("MASK",),
                    "images2_opt": ("IMAGE",),
                    "mask2_opt": ("MASK",),
                    "images3_opt": ("IMAGE",),
                    "mask3_opt": ("MASK",),
                    "images4_opt": ("IMAGE",),
                    "mask4_opt": ("MASK",),
                    },
                }

    RETURN_TYPES = ("IMAGE", "MASK", )

    OUTPUT_NODE = True

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, select, images1, mask1_opt=None, images2_opt=None, mask2_opt=None, images3_opt=None, mask3_opt=None, images4_opt=None, mask4_opt=None):
        if select == 1:
            return images1, mask1_opt,
        elif select == 2:
            return images2_opt, mask2_opt,
        elif select == 3:
            return images3_opt, mask3_opt,
        else:
            return images4_opt, mask4_opt,


class LatentSwitch:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "select": ("INT", {"default": 1, "min": 1, "max": 99999, "step": 1}),
                    "latent1": ("LATENT",),
                    },
                }

    RETURN_TYPES = ("LATENT", )

    OUTPUT_NODE = True

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, *args, **kwargs):
        input_name = f"latent{int(kwargs['select'])}"

        if input_name in kwargs:
            return (kwargs[input_name],)
        else:
            print(f"LatentSwitch: invalid select index ('latent1' is selected)")
            return (kwargs['latent1'],)


class SaveConditioning:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"conditioning": ("CONDITIONING", ),
                             "filename_prefix": ("STRING", {"default": "conditioning/ComfyUI"}),
                             },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "doit"

    OUTPUT_NODE = True

    CATEGORY = "_for_testing"

    def doit(self, conditioning, filename_prefix, prompt=None, extra_pnginfo=None):
        # support save metadata for latent sharing
        prompt_info = ""
        if prompt is not None:
            prompt_info = json.dumps(prompt)

        for tensor_data, meta_data in conditioning:
            full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir)

            metadata = {"prompt": prompt_info}
            if extra_pnginfo is not None:
                for x in extra_pnginfo:
                    metadata[x] = json.dumps(extra_pnginfo[x])

            file = f"{filename}_{counter:05}_.conditioning"
            file = os.path.join(full_output_folder, file)

            print(f"meta_data:{meta_data}")
            print(f"tensor_data:{tensor_data}")

            output = {"conditioning": tensor_data}
            metadata['conditioning_aux'] = json.dumps(meta_data)

            safetensors.torch.save_file(output, file, metadata=metadata)

        return {}


class LoadConditioning:
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f)) and f.endswith(".conditioning")]
        return {"required": {"conditioning": [sorted(files), ]}, }

    CATEGORY = "_for_testing"

    RETURN_TYPES = ("CONDITIONING", )
    FUNCTION = "load"

    def load(self, conditioning):
        conditioning_path = folder_paths.get_annotated_filepath(conditioning)
        data = safetensors.torch.load_file(conditioning_path, device="cpu")
        return ([[data['conditioning'], {}]], )

    @classmethod
    def IS_CHANGED(s, conditioning):
        image_path = folder_paths.get_annotated_filepath(conditioning)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(s, conditioning):
        if not folder_paths.exists_annotated_filepath(conditioning):
            return "Invalid conditioning file: {}".format(conditioning)
        return True


class ImpactWildcardProcessor:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "wildcard_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                        "populated_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                        "mode": ("BOOLEAN", {"default": True, "label_on": "Populate", "label_off": "Fixed"}),
                        "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    },
                }

    CATEGORY = "ImpactPack/Prompt"

    RETURN_TYPES = ("STRING", )
    FUNCTION = "doit"

    def doit(self, wildcard_text, populated_text, mode, seed):
        return (populated_text, )


class ImpactWildcardEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "model": ("MODEL",),
                        "clip": ("CLIP",),
                        "wildcard_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                        "populated_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                        "mode": ("BOOLEAN", {"default": True, "label_on": "Populate", "label_off": "Fixed"}),
                        "Select to add LoRA": (["Select the LoRA to add to the text"] + folder_paths.get_filename_list("loras"), ),
                        "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    },
                }

    CATEGORY = "ImpactPack/Prompt"

    RETURN_TYPES = ("MODEL", "CLIP", "CONDITIONING", "STRING")
    RETURN_NAMES = ("model", "clip", "conditioning", "populated_text")
    FUNCTION = "doit"

    def doit(self, *args, **kwargs):
        populated = kwargs['populated_text']
        model, clip, conditioning = impact.wildcards.process_with_loras(populated, kwargs['model'], kwargs['clip'])
        return (model, clip, conditioning, populated)


class ReencodeLatent:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "samples": ("LATENT", ),
                        "tile_mode": (["None", "Both", "Decode(input) only", "Encode(output) only"],),
                        "input_vae": ("VAE", ),
                        "output_vae": ("VAE", ),
                        "tile_size": ("INT", {"default": 512, "min": 320, "max": 4096, "step": 64}),
                    },
                }

    CATEGORY = "ImpactPack/Util"

    RETURN_TYPES = ("LATENT", )
    FUNCTION = "doit"

    def doit(self, samples, tile_mode, input_vae, output_vae, tile_size=512):
        if tile_mode in ["Both", "Decode(input) only"]:
            pixels = nodes.VAEDecodeTiled().decode(input_vae, samples, tile_size)[0]
        else:
            pixels = nodes.VAEDecode().decode(input_vae, samples)[0]

        if tile_mode in ["Both", "Encode(output) only"]:
            return nodes.VAEEncodeTiled().encode(output_vae, pixels, tile_size)
        else:
            return nodes.VAEEncode().encode(output_vae, pixels)


class ReencodeLatentPipe:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "samples": ("LATENT", ),
                        "tile_mode": (["None", "Both", "Decode(input) only", "Encode(output) only"],),
                        "input_basic_pipe": ("BASIC_PIPE", ),
                        "output_basic_pipe": ("BASIC_PIPE", ),
                    },
                }

    CATEGORY = "ImpactPack/Util"

    RETURN_TYPES = ("LATENT", )
    FUNCTION = "doit"

    def doit(self, samples, tile_mode, input_basic_pipe, output_basic_pipe):
        _, _, input_vae, _, _ = input_basic_pipe
        _, _, output_vae, _, _ = output_basic_pipe
        return ReencodeLatent().doit(samples, tile_mode, input_vae, output_vae)


class ImageBatchToImageList:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image": ("IMAGE",), }}

    RETURN_TYPES = ("IMAGE",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, image):
        images = [image[i:i + 1, ...] for i in range(image.shape[0])]
        return (images, )


class MakeImageList:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image1": ("IMAGE",), }}

    RETURN_TYPES = ("IMAGE",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, **kwargs):
        images = []

        for k, v in kwargs.items():
            images.append(v)

        return (images, )


class StringSelector:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "strings": ("STRING", {"multiline": True}),
            "multiline": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
            "select": ("INT", {"min": 0, "max": sys.maxsize, "step": 1, "default": 0}),
        }}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, strings, multiline, select):
        lines = strings.split('\n')

        if multiline:
            result = []
            current_string = ""

            for line in lines:
                if line.startswith("#"):
                    if current_string:
                        result.append(current_string.strip())
                        current_string = ""
                current_string += line + "\n"

            if current_string:
                result.append(current_string.strip())

            if len(result) == 0:
                selected = strings
            else:
                selected = result[select % len(result)]

            if selected.startswith('#'):
                selected = selected[1:]
        else:
            if len(lines) == 0:
                selected = strings
            else:
                selected = lines[select % len(lines)]

        return (selected, )
