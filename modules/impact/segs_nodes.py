import os
import sys

import folder_paths
import comfy
from nodes import MAX_RESOLUTION

from impact.utils import *
import impact.core as core
from impact.core import SEG

class SEGSDetailer:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "segs": ("SEGS", ),
                     "guide_size": ("FLOAT", {"default": 256, "min": 64, "max": MAX_RESOLUTION, "step": 8}),
                     "guide_size_for": ("BOOLEAN", {"default": True, "label_on": "bbox", "label_off": "crop_region"}),
                     "max_size": ("FLOAT", {"default": 768, "min": 64, "max": MAX_RESOLUTION, "step": 8}),
                     "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                     "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                     "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                     "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                     "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                     "denoise": ("FLOAT", {"default": 0.5, "min": 0.0001, "max": 1.0, "step": 0.01}),
                     "noise_mask": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
                     "force_inpaint": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                     "basic_pipe": ("BASIC_PIPE",),
                     "refiner_ratio": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0}),
                     },
                "optional": {
                     "refiner_basic_pipe_opt": ("BASIC_PIPE",),
                    }
                }

    RETURN_TYPES = ("SEGS", "IMAGE")
    RETURN_NAMES = ("segs", "cnet_images")
    OUTPUT_IS_LIST = (False, True)

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    @staticmethod
    def do_detail(image, segs, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
                  denoise, noise_mask, force_inpaint, basic_pipe, refiner_ratio=None, refiner_basic_pipe_opt=None):

        model, clip, vae, positive, negative = basic_pipe
        if refiner_basic_pipe_opt is None:
            refiner_model, refiner_clip, refiner_positive, refiner_negative = None, None, None, None
        else:
            refiner_model, refiner_clip, _, refiner_positive, refiner_negative = refiner_basic_pipe_opt

        new_segs = []
        cnet_pil_list = []

        for seg in segs[1]:
            cropped_image = seg.cropped_image if seg.cropped_image is not None \
                                              else crop_ndarray4(image.numpy(), seg.crop_region)

            is_mask_all_zeros = (seg.cropped_mask == 0).all().item()
            if is_mask_all_zeros:
                print(f"Detailer: segment skip [empty mask]")
                new_segs.append(seg)
                continue

            if noise_mask:
                cropped_mask = seg.cropped_mask
            else:
                cropped_mask = None

            enhanced_pil, cnet_pil = core.enhance_detail(cropped_image, model, clip, vae, guide_size, guide_size_for, max_size,
                                                         seg.bbox, seed, steps, cfg, sampler_name, scheduler,
                                                         positive, negative, denoise, cropped_mask, force_inpaint,
                                                         refiner_ratio=refiner_ratio, refiner_model=refiner_model,
                                                         refiner_clip=refiner_clip, refiner_positive=refiner_positive, refiner_negative=refiner_negative,
                                                         control_net_wrapper=seg.control_net_wrapper)

            if cnet_pil is not None:
                cnet_pil_list.append(cnet_pil)

            if enhanced_pil is None:
                new_cropped_image = cropped_image
            else:
                new_cropped_image = pil2numpy(enhanced_pil)

            new_seg = SEG(new_cropped_image, seg.cropped_mask, seg.confidence, seg.crop_region, seg.bbox, seg.label, None)
            new_segs.append(new_seg)

        return (segs[0], new_segs), cnet_pil_list

    def doit(self, image, segs, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name, scheduler,
             denoise, noise_mask, force_inpaint, basic_pipe, refiner_ratio=None, refiner_basic_pipe_opt=None):

        segs, cnet_pil_list = SEGSDetailer.do_detail(image, segs, guide_size, guide_size_for, max_size, seed, steps, cfg, sampler_name,
                                                     scheduler, denoise, noise_mask, force_inpaint, basic_pipe, refiner_ratio, refiner_basic_pipe_opt)

        # set fallback image
        if len(cnet_pil_list) == 0:
            cnet_pil_list = [empty_pil_tensor()]

        return (segs, cnet_pil_list)


class SEGSPaste:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "image": ("IMAGE", ),
                     "segs": ("SEGS", ),
                     "feather": ("INT", {"default": 5, "min": 0, "max": 100, "step": 1}),
                     },
                "optional": {"ref_image_opt": ("IMAGE", ),}
                }

    RETURN_TYPES = ("IMAGE", )
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Detailer"

    @staticmethod
    def doit(image, segs, feather, ref_image_opt=None):
        image_pil = tensor2pil(image).convert('RGBA')

        for seg in segs[1]:
            ref_image_pil = None
            if ref_image_opt is None and seg.cropped_image is not None:
                ref_image_pil = tensor2pil(torch.from_numpy(seg.cropped_image))
            elif ref_image_opt is not None:
                cropped = crop_image(ref_image_opt, seg.crop_region)
                cropped = np.clip(255. * cropped.squeeze(), 0, 255).astype(np.uint8)
                ref_image_pil = Image.fromarray(cropped).convert('RGBA')

            if ref_image_pil is not None:
                mask_pil = feather_mask(seg.cropped_mask, feather)
                image_pil.paste(ref_image_pil, (seg.crop_region[0], seg.crop_region[1]), mask_pil)

        image_tensor = pil2tensor(image_pil.convert('RGB'))

        return (image_tensor, )


class SEGSPreview:
    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs": ("SEGS", ),
                     },
                "optional": {
                     "fallback_image_opt": ("IMAGE", ),
                    }
                }

    RETURN_TYPES = ()
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    OUTPUT_NODE = True

    def doit(self, segs, fallback_image_opt=None):
        full_output_folder, filename, counter, subfolder, filename_prefix = \
            folder_paths.get_save_image_path("impact_seg_preview", self.output_dir, segs[0][1], segs[0][0])

        results = list()

        for seg in segs[1]:
            cropped_image = None

            if seg.cropped_image is not None:
                cropped_image = seg.cropped_image
            elif fallback_image_opt is not None:
                # take from original image
                cropped_image = crop_image(fallback_image_opt, seg.crop_region)

            if cropped_image is not None:
                cropped_image = Image.fromarray(np.clip(255. * cropped_image.squeeze(), 0, 255).astype(np.uint8))

                file = f"{filename}_{counter:05}_.webp"
                cropped_image.save(os.path.join(full_output_folder, file))
                results.append({
                    "filename": file,
                    "subfolder": subfolder,
                    "type": self.type
                })

                counter += 1

        return {"ui": {"images": results}}


class SEGSLabelFilter:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "segs": ("SEGS", ),
                        "preset": ([
                                       'all', 'hand', 'face', 'mouth', 'eyes', 'eyebrows', 'pupils',
                                       'left_eyebrow', 'left_eye', 'left_pupil',
                                       'right_eyebrow', 'right_eye', 'right_pupil',
                                       'short_sleeved_shirt',
                                       'long_sleeved_shirt', 'short_sleeved_outwear', 'long_sleeved_outwear',
                                       'vest', 'sling', 'shorts', 'trousers', 'skirt', 'short_sleeved_dress',
                                       'long_sleeved_dress', 'vest_dress', 'sling_dress'], ),
                        "labels": ("STRING", {"multiline": True, "placeholder": "List the types of segments to be allowed, separated by commas"}),
                     },
                }

    RETURN_TYPES = ("SEGS", "SEGS",)
    RETURN_NAMES = ("filtered_SEGS", "remained_SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs, preset, labels):
        labels = labels.split(',')
        labels = set([label.strip() for label in labels])

        if 'all' in labels:
            return (segs, (segs[0], []), )
        else:
            res_segs = []
            remained_segs = []

            for x in segs[1]:
                if x.label in labels:
                    res_segs.append(x)
                elif 'eyes' in labels and x.label in ['left_eye', 'right_eye']:
                    res_segs.append(x)
                elif 'eyebrows' in labels and x.label in ['left_eyebrow', 'right_eyebrow']:
                    res_segs.append(x)
                elif 'pupils' in labels and x.label in ['left_pupil', 'right_pupil']:
                    res_segs.append(x)
                else:
                    remained_segs.append(x)

        return ((segs[0], res_segs), (segs[0], remained_segs), )


class SEGSOrderedFilter:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "segs": ("SEGS", ),
                        "target": (["area(=w*h)", "width", "height", "x1", "y1", "x2", "y2"],),
                        "order": ("BOOLEAN", {"default": True, "label_on": "descending", "label_off": "ascending"}),
                        "take_start": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}),
                        "take_count": ("INT", {"default": 1, "min": 0, "max": sys.maxsize, "step": 1}),
                     },
                }

    RETURN_TYPES = ("SEGS", "SEGS",)
    RETURN_NAMES = ("filtered_SEGS", "remained_SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs, target, order, take_start, take_count):
        segs_with_order = []

        for seg in segs[1]:
            x1 = seg.crop_region[0]
            y1 = seg.crop_region[1]
            x2 = seg.crop_region[2]
            y2 = seg.crop_region[3]

            if target == "area(=w*h)":
                value = (y2 - y1) * (x2 - x1)
            elif target == "width":
                value = x2 - x1
            elif target == "height":
                value = y2 - y1
            elif target == "x1":
                value = x1
            elif target == "x2":
                value = x2
            elif target == "y1":
                value = y1
            else:
                value = y2

            segs_with_order.append((value, seg))

        if order:
            sorted_list = sorted(segs_with_order, key=lambda x: x[0], reverse=True)
        else:
            sorted_list = sorted(segs_with_order, key=lambda x: x[0], reverse=False)

        result_list = []
        remained_list = []

        for i, item in enumerate(sorted_list):
            if take_start <= i < take_start + take_count:
                result_list.append(item[1])
            else:
                remained_list.append(item[1])

        return ((segs[0], result_list), (segs[0], remained_list), )


class SEGSRangeFilter:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                        "segs": ("SEGS", ),
                        "target": (["area(=w*h)", "width", "height", "x1", "y1", "x2", "y2", "length_percent"],),
                        "mode": ("BOOLEAN", {"default": True, "label_on": "inside", "label_off": "outside"}),
                        "min_value": ("INT", {"default": 0, "min": 0, "max": sys.maxsize, "step": 1}),
                        "max_value": ("INT", {"default": 67108864, "min": 0, "max": sys.maxsize, "step": 1}),
                     },
                }

    RETURN_TYPES = ("SEGS", "SEGS",)
    RETURN_NAMES = ("filtered_SEGS", "remained_SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs, target, mode, min_value, max_value):
        new_segs = []
        remained_segs = []

        for seg in segs[1]:
            x1 = seg.crop_region[0]
            y1 = seg.crop_region[1]
            x2 = seg.crop_region[2]
            y2 = seg.crop_region[3]

            if target == "area(=w*h)":
                value = (y2 - y1) * (x2 - x1)
            elif target == "length_percent":
                h = y2 - y1
                w = x2 - x1
                value = max(h/w, w/h)*100
                print(f"value={value}")
            elif target == "width":
                value = x2 - x1
            elif target == "height":
                value = y2 - y1
            elif target == "x1":
                value = x1
            elif target == "x2":
                value = x2
            elif target == "y1":
                value = y1
            else:
                value = y2

            if mode and min_value <= value <= max_value:
                print(f"[in] value={value} / {mode}, {min_value}, {max_value}")
                new_segs.append(seg)
            elif not mode and (value < min_value or value > max_value):
                print(f"[out] value={value} / {mode}, {min_value}, {max_value}")
                new_segs.append(seg)
            else:
                remained_segs.append(seg)
                print(f"[filter] value={value} / {mode}, {min_value}, {max_value}")

        return ((segs[0], new_segs), (segs[0], remained_segs), )


class SEGSToImageList:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs": ("SEGS", ),
                     },
                "optional": {
                     "fallback_image_opt": ("IMAGE", ),
                    }
                }

    RETURN_TYPES = ("IMAGE",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs, fallback_image_opt=None):
        results = list()

        for seg in segs[1]:
            if seg.cropped_image is not None:
                cropped_image = torch.from_numpy(seg.cropped_image)
            elif fallback_image_opt is not None:
                # take from original image
                cropped_image = torch.from_numpy(crop_image(fallback_image_opt, seg.crop_region))
            else:
                cropped_image = empty_pil_tensor()

            results.append(cropped_image)

        if len(results) == 0:
            results.append(empty_pil_tensor())

        return (results,)


class SEGSToMaskList:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs": ("SEGS", ),
                     },
                }

    RETURN_TYPES = ("MASK",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs):
        masks = core.segs_to_masklist(segs)
        return (masks,)


class SEGSToMaskBatch:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs": ("SEGS", ),
                     },
                }

    RETURN_TYPES = ("MASKS",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs):
        masks = core.segs_to_masklist(segs)
        mask_batch = torch.stack(masks, dim=0)
        return (mask_batch,)


class SEGSConcat:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs1": ("SEGS", ),
                     "segs2": ("SEGS", ),
                     },
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs1, segs2):
        if segs1[0] == segs2[0]:
            return ((segs1[0], segs1[1] + segs2[1]), )
        else:
            print(f"ERROR: source shape of 'segs1' and 'segs2' are different. 'segs2' will be ignored")
            return (segs1, )


class DecomposeSEGS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "segs": ("SEGS", ),
                     },
                }

    RETURN_TYPES = ("SEGS_HEADER", "SEG_ELT",)
    OUTPUT_IS_LIST = (False, True, )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs):
        return segs


class AssembleSEGS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "seg_header": ("SEGS_HEADER", ),
                     "seg_elt": ("SEG_ELT", ),
                     },
                }

    INPUT_IS_LIST = True

    RETURN_TYPES = ("SEGS", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, seg_header, seg_elt):
        return ((seg_header[0], seg_elt), )


class From_SEG_ELT:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "seg_elt": ("SEG_ELT", ),
                     },
                }

    RETURN_TYPES = ("SEG_ELT", "IMAGE", "MASK", "SEG_ELT_crop_region", "SEG_ELT_bbox", "SEG_ELT_control_net_wrapper", "FLOAT", "STRING")
    RETURN_NAMES = ("seg_elt", "cropped_image", "cropped_mask", "crop_region", "bbox", "control_net_wrapper", "confidence", "label")

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, seg_elt):
        cropped_image = torch.tensor(seg_elt.cropped_image) if seg_elt.cropped_image is not None else None
        return (seg_elt, cropped_image, torch.tensor(seg_elt.cropped_mask), seg_elt.crop_region, seg_elt.bbox, seg_elt.control_net_wrapper, seg_elt.confidence, seg_elt.label,)


class Edit_SEG_ELT:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "seg_elt": ("SEG_ELT", ),
                     },
                "optional": {
                     "cropped_image_opt": ("IMAGE", ),
                     "cropped_mask_opt": ("MASK", ),
                     "crop_region_opt": ("SEG_ELT_crop_region", ),
                     "bbox_opt": ("SEG_ELT_bbox", ),
                     "control_net_wrapper_opt": ("SEG_ELT_control_net_wrapper", ),
                     "confidence_opt": ("FLOAT", {"min": 0, "max": 1.0, "step": 0.1, "forceInput": True}),
                     "label_opt": ("STRING", {"multiline": False, "forceInput": True}),
                    }
                }

    RETURN_TYPES = ("SEG_ELT", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, seg_elt, cropped_image_opt=None, cropped_mask_opt=None, confidence_opt=None, crop_region_opt=None,
             bbox_opt=None, label_opt=None, control_net_wrapper_opt=None):

        cropped_image = seg_elt.cropped_image if cropped_image_opt is None else cropped_image_opt
        cropped_mask = seg_elt.cropped_mask if cropped_mask_opt is None else cropped_mask_opt
        confidence = seg_elt.confidence if confidence_opt is None else confidence_opt
        crop_region = seg_elt.crop_region if crop_region_opt is None else crop_region_opt
        bbox = seg_elt.bbox if bbox_opt is None else bbox_opt
        label = seg_elt.label if label_opt is None else label_opt
        control_net_wrapper = seg_elt.control_net_wrapper if control_net_wrapper_opt is None else control_net_wrapper_opt

        cropped_image = cropped_image.numpy() if cropped_image is not None else None
        seg = SEG(cropped_image, cropped_mask.numpy(), confidence, crop_region, bbox, label, control_net_wrapper)

        return (seg,)


class DilateMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "mask": ("MASK", ),
                     "dilation": ("INT", {"default": 10, "min": -512, "max": 512, "step": 1}),
                }}

    RETURN_TYPES = ("MASK", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, mask, dilation):
        mask = core.dilate_mask(mask.numpy(), dilation)
        return (torch.from_numpy(mask), )


class Dilate_SEG_ELT:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "seg_elt": ("SEG_ELT", ),
                     "dilation": ("INT", {"default": 10, "min": -512, "max": 512, "step": 1}),
                }}

    RETURN_TYPES = ("SEG_ELT", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, seg, dilation):
        mask = core.dilate_mask(seg.cropped_mask, dilation)
        seg = SEG(seg.cropped_image, mask, seg.confidence, seg.crop_region, seg.bbox, seg.label, seg.control_net_wrapper)
        return (seg,)


class SEG_ELT_BBOX_ScaleBy:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                     "seg": ("SEG_ELT", ),
                     "scale_by": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 8.0, "step": 0.01}), }
                }

    RETURN_TYPES = ("SEG_ELT", )

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    @staticmethod
    def fill_zero_outside_bbox(mask, crop_region, bbox):
        cx1, cy1, _, _ = crop_region
        x1, y1, x2, y2 = bbox
        x1, y1, x2, y2 = x1-cx1, y1-cy1, x2-cx1, y2-cy1
        h, w = mask.shape

        x1 = min(w-1, max(0, x1))
        x2 = min(w-1, max(0, x2))
        y1 = min(h-1, max(0, y1))
        y2 = min(h-1, max(0, y2))

        mask_cropped = mask.copy()
        mask_cropped[:, :x1] = 0  # zero fill left side
        mask_cropped[:, x2:] = 0  # zero fill right side
        mask_cropped[:y1, :] = 0  # zero fill top side
        mask_cropped[y2:, :] = 0  # zero fill bottom side
        return mask_cropped

    def doit(self, seg, scale_by):
        x1, y1, x2, y2 = seg.bbox
        w = x2-x1
        h = y2-y1

        dw = int((w * scale_by - w)/2)
        dh = int((h * scale_by - h)/2)

        bbox = (x1-dw, y1-dh, x2+dw, y2+dh)

        cropped_mask = SEG_ELT_BBOX_ScaleBy.fill_zero_outside_bbox(seg.cropped_mask, seg.crop_region, bbox)
        seg = SEG(seg.cropped_image, cropped_mask, seg.confidence, seg.crop_region, bbox, seg.label, seg.control_net_wrapper)
        return (seg,)


class EmptySEGS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {}, }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self):
        shape = 0, 0
        return ((shape, []),)


class SegsToCombinedMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "segs": ("SEGS",), } }

    RETURN_TYPES = ("MASK",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, segs):
        return (core.segs_to_combined_mask(segs),)


class MediaPipeFaceMeshToSEGS:
    @classmethod
    def INPUT_TYPES(s):
        bool_true_widget = ("BOOLEAN", {"default": True, "label_on": "Enabled", "label_off": "Disabled"})
        bool_false_widget = ("BOOLEAN", {"default": False, "label_on": "Enabled", "label_off": "Disabled"})
        return {"required": {
                                "image": ("IMAGE",),
                                "crop_factor": ("FLOAT", {"default": 3.0, "min": 1.0, "max": 10, "step": 0.1}),
                                "bbox_fill": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                                "crop_min_size": ("INT", {"min": 10, "max": MAX_RESOLUTION, "step": 1, "default": 50}),
                                "drop_size": ("INT", {"min": 1, "max": MAX_RESOLUTION, "step": 1, "default": 1}),
                                "dilation": ("INT", {"default": 0, "min": -512, "max": 512, "step": 1}),
                                "face": bool_true_widget,
                                "mouth": bool_false_widget,
                                "left_eyebrow": bool_false_widget,
                                "left_eye": bool_false_widget,
                                "left_pupil": bool_false_widget,
                                "right_eyebrow": bool_false_widget,
                                "right_eye": bool_false_widget,
                                "right_pupil": bool_false_widget,
                             },
                # "optional": {"reference_image_opt": ("IMAGE", ), }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, image, crop_factor, bbox_fill, crop_min_size, drop_size, dilation, face, mouth, left_eyebrow, left_eye, left_pupil, right_eyebrow, right_eye, right_pupil):
        # padding is obsolete now
        # https://github.com/Fannovel16/comfyui_controlnet_aux/blob/1ec41fceff1ee99596445a0c73392fd91df407dc/utils.py#L33
        # def calc_pad(h_raw, w_raw):
        #     resolution = normalize_size_base_64(h_raw, w_raw)
        #
        #     def pad64(x):
        #         return int(np.ceil(float(x) / 64.0) * 64 - x)
        #
        #     k = float(resolution) / float(min(h_raw, w_raw))
        #     h_target = int(np.round(float(h_raw) * k))
        #     w_target = int(np.round(float(w_raw) * k))
        #
        #     return pad64(h_target), pad64(w_target)

        # if reference_image_opt is not None:
        #     if image.shape[1:] != reference_image_opt.shape[1:]:
        #         scale_by1 = reference_image_opt.shape[1] / image.shape[1]
        #         scale_by2 = reference_image_opt.shape[2] / image.shape[2]
        #         scale_by = min(scale_by1, scale_by2)
        #
        #         # padding is obsolete now
        #         # h_pad, w_pad = calc_pad(reference_image_opt.shape[1], reference_image_opt.shape[2])
        #         # if h_pad != 0:
        #         #     # height padded
        #         #     image = image[:, :-h_pad, :, :]
        #         # elif w_pad != 0:
        #         #     # width padded
        #         #     image = image[:, :, :-w_pad, :]
        #
        #         image = nodes.ImageScaleBy().upscale(image, "bilinear", scale_by)[0]

        result = core.mediapipe_facemesh_to_segs(image, crop_factor, bbox_fill, crop_min_size, drop_size, dilation, face, mouth, left_eyebrow, left_eye, left_pupil, right_eyebrow, right_eye, right_pupil)
        return (result, )


class MaskToSEGS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                                "mask": ("MASK",),
                                "combined": ("BOOLEAN", {"default": False, "label_on": "True", "label_off": "False"}),
                                "crop_factor": ("FLOAT", {"default": 3.0, "min": 1.0, "max": 10, "step": 0.1}),
                                "bbox_fill": ("BOOLEAN", {"default": False, "label_on": "enabled", "label_off": "disabled"}),
                                "drop_size": ("INT", {"min": 1, "max": MAX_RESOLUTION, "step": 1, "default": 10}),
                             }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Operation"

    def doit(self, mask, combined, crop_factor, bbox_fill, drop_size):
        result = core.mask_to_segs(mask, combined, crop_factor, bbox_fill, drop_size)
        return (result, )


class ControlNetApplySEGS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "segs": ("SEGS",),
                    "control_net": ("CONTROL_NET",),
                    "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                    },
                "optional": {
                    "segs_preprocessor": ("SEGS_PREPROCESSOR",),
                    }
                }

    RETURN_TYPES = ("SEGS",)
    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, segs, control_net, strength, segs_preprocessor=None):
        new_segs = []

        for seg in segs[1]:
            control_net_wrapper = core.ControlNetWrapper(control_net, strength, segs_preprocessor)
            new_seg = SEG(seg.cropped_image, seg.cropped_mask, seg.confidence, seg.crop_region, seg.bbox, seg.label, control_net_wrapper)
            new_segs.append(new_seg)

        return ((segs[0], new_segs), )


class SEGSSwitch:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "select": ("INT", {"default": 1, "min": 1, "max": 99999, "step": 1}),
                    "segs1": ("SEGS",),
                    },
                }

    RETURN_TYPES = ("SEGS", )

    OUTPUT_NODE = True

    FUNCTION = "doit"

    CATEGORY = "ImpactPack/Util"

    def doit(self, *args, **kwargs):
        input_name = f"segs{int(kwargs['select'])}"

        if input_name in kwargs:
            return (kwargs[input_name],)
        else:
            print(f"SEGSSwitch: invalid select index ('segs1' is selected)")
            return (kwargs['segs1'],)
