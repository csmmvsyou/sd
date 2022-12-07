import os
import uuid
import random
import argparse

from PIL import Image
from pathlib import Path
from typing import List, Optional
from custom_inpainting.inpainting import Inpainter
from custom_inpainting.inpainting_area import InpaintingArea, InpaintingAreaGenerator, InpaintingAreaGeneratorCOCO, InpaintingAreaGeneratorRandom
from custom_inpainting.prompt_generation import PromptGenerator
from custom_inpainting.utils import insert_image
from torchvision.transforms.functional import crop
from tqdm import tqdm

        
class InpaintingPipeline:

    def __init__(
        self,
        inpainter: Inpainter,
        prompt_generator: PromptGenerator,
        inpainting_area_generator: InpaintingAreaGenerator,
        generation_limit: int,
        logs_file_path: str
    ) -> None:
        self.inpainter = inpainter # How to draw
        self.prompt_generator = prompt_generator # What to draw
        self.inpainting_area_generator = inpainting_area_generator # Where to draw
        self.generation_limit = generation_limit
        self.logs_file_path = Path(logs_file_path)

        assert inpainting_area_generator.context_bbox_size % inpainter.input_stride == 0, \
            f"Specified context_bbox_size {inpainting_area_generator.context_bbox_size} is not a multiple of the model input stride {inpainter.input_stride}"
    
    def run(
        self,
        input_images_dir: str,
        result_images_dir: str,    
    ):  
        os.makedirs(result_images_dir, exist_ok=True)
        img_paths: List[Path] = self._get_img_paths(input_images_dir)
        for img_path in tqdm(img_paths):

            inpainting_areas = self.inpainting_area_generator.get_inpainting_areas(img_path)
            if len(inpainting_areas) == 0:
                print(f'Skipping the image {img_path.name} because there is no inpainting areas on it')
                continue
            prompt=self.prompt_generator.generate_prompt()

            inpainted_image = self.process_image(
                image=Image.open(img_path),
                inpainting_areas=inpainting_areas,
                prompt=prompt
            )

            inpainted_image.save(self._get_inpainted_image_path(
                inpainted_images_dir=result_images_dir, 
                img_name=img_path.name
            ))

            with self.logs_file_path.open('a', encoding='utf-8') as f:
                f.write(f'{img_path.name}, "{prompt}"\n')

    def process_image(
        self,
        image: Image.Image,
        inpainting_areas: List[InpaintingArea],
        prompt: str
    ) -> Image.Image:
        for inpainting_area in inpainting_areas:
            image = self.process_inpainting_area(
                image=image,
                inpainting_area=inpainting_area,
                prompt=prompt
            )
        return image

    def process_inpainting_area(
        self,
        image: Image.Image,
        inpainting_area: InpaintingArea,
        prompt: str
    ) -> Image.Image:  
        # Crop image by context_bbox
        context_img = crop(
            image, 
            inpainting_area.context_bbox.y1, 
            inpainting_area.context_bbox.x1, 
            inpainting_area.context_bbox.h, 
            inpainting_area.context_bbox.w
        )

        # Create inpainting mask
        inpainting_mask = inpainting_area.get_inpainting_mask()


        samples_dir = "/home/vv/stable-diffusion-2/test_dir/results"
        sample_masks_dir = "/home/vv/stable-diffusion-2/test_dir/masks"

        os.makedirs(samples_dir, exist_ok=True)
        os.makedirs(sample_masks_dir, exist_ok=True)

        inpainting_mask.save(f'{sample_masks_dir}/{random.randint(1000, 9999)}.jpg')

        # Resize to the model input size
        inpainting_mask = inpainting_mask.resize((self.inpainter.input_width, self.inpainter.input_width), resample=Image.Resampling.NEAREST)
        context_img = context_img.resize((self.inpainter.input_width, self.inpainter.input_width), resample=Image.Resampling.BILINEAR)

        # Inpaint
        inpainted_crop = self.inpainter.inpaint(
            image=context_img,
            mask=inpainting_mask,
            prompt=prompt,
        )

        # Resize result to the context_bbox size
        inpainted_crop = inpainted_crop.resize((inpainting_area.context_bbox.h, inpainting_area.context_bbox.w), resample=Image.Resampling.BILINEAR)

        inpainted_crop.save(f'{samples_dir}/{random.randint(1000, 9999)}.jpg')

        # Insert inpainted crop to the image
        return insert_image(
            src_image=image, 
            img_to_insert=inpainted_crop, 
            x=inpainting_area.context_bbox.x1, 
            y=inpainting_area.context_bbox.y1, 
        )
    
    def _get_img_paths(self, input_images_dir: str) -> List[Path]:
        # Shuffles and selects img paths
        img_paths_in_dir = [img_path for img_path in Path(input_images_dir).iterdir()]
        random.shuffle(img_paths_in_dir)
        selected_paths = list()
        for i in range(self.generation_limit // len(img_paths_in_dir)):
            selected_paths.extend(img_paths_in_dir)
        selected_paths.extend(img_paths_in_dir[: self.generation_limit % len(img_paths_in_dir)])
        return selected_paths

    @staticmethod
    def _get_inpainted_image_path(inpainted_images_dir: str, img_name: str) -> Path:
        random_uid = str(uuid.uuid4())[:8]
        base_name, ext = os.path.splitext(img_name)
        result_img_name = f"{base_name}_{random_uid}{ext}"
        return Path(inpainted_images_dir).joinpath(result_img_name)


def main(
    input_images_dir: str,
    result_images_dir: str,

    base_prompt: str, 
    context_bbox_size: int,
    generation_limit: int,
    logs_file_path: str,

    coco_ann_path: str = None,
    coco_bbox_padding: int = 0,

    inpaint_box_size: int = None, 
    number_of_areas_per_image: int = 1,

    config_path: Optional[str] = None,
    weights_path: Optional[str] = None,
    half_model: bool = False,
    num_inference_steps: int = 60,
    tags_txt_path: str = None, 
    number_of_tags_per_prompt: int = 1
):
    
    if coco_ann_path is not None:
        inpainting_area_generator = InpaintingAreaGeneratorCOCO(
            context_bbox_size=context_bbox_size,
            coco_ann_path=coco_ann_path,
            img_dir=input_images_dir,
            padding=coco_bbox_padding,
        )
    else:
        assert inpaint_box_size is not None
        inpainting_area_generator = InpaintingAreaGeneratorRandom(
            context_bbox_size=context_bbox_size,
            inpaint_box_size=inpaint_box_size, 
            number_of_areas_per_image=number_of_areas_per_image
        )

    pipeline = InpaintingPipeline(
        inpainter=Inpainter(
            config_path=config_path,
            weights_path=weights_path,
            half_model=half_model,
            num_inference_steps=num_inference_steps,
        ),
        prompt_generator=PromptGenerator(
            base_prompt=base_prompt, 
            tags_txt_path=tags_txt_path, 
            number_of_tags_per_prompt=number_of_tags_per_prompt
        ),
        inpainting_area_generator=inpainting_area_generator,
        generation_limit=generation_limit,
        logs_file_path=logs_file_path
    )

    pipeline.run(
        input_images_dir=input_images_dir,
        result_images_dir=result_images_dir
    )


def parce_args() -> argparse.Namespace:
    args = argparse.ArgumentParser()
    args.add_argument('--input_images_dir', type=str, required=True)
    args.add_argument('--result_images_dir', type=str, required=True)
    args.add_argument('--generation_limit', type=int, default=100)

    args.add_argument('--base_prompt', type=str, required=True)
    args.add_argument('--context_bbox_size', type=int, required=True)
    args.add_argument('--logs_file_path', type=str, required=True)

    args.add_argument('--coco_ann_path', type=str)
    args.add_argument('--coco_bbox_padding', type=int)



    args.add_argument('--coco_bbox_padding', type=int)
    args.add_argument('--coco_bbox_padding', type=int, default=1)


    args.add_argument('--generated_images_dir', type=str, required=True)
    args.add_argument('--prompts_file_path', type=str, required=True)
    args.add_argument('--config_path', type=str, default=None)
    args.add_argument('--weights_path', type=str, default=None)
    args.add_argument('--half_model', action='store_true')
    args.add_argument('--crop_size', type=int, default=512)
    args.add_argument('--inference_resize', type=int, default=None)
    args.add_argument('--num_infer_steps', type=int, default=60)
    args.add_argument('--generate_prompt', action='store_true')


    input_images_dir="/media/data/vv/tasks/2022_12_06_test_inpainting/samples",
    result_images_dir="/media/data/vv/tasks/2022_12_05_debug_stable_diffusion2/generated/test3",
    generation_limit=10,

    base_prompt="head", 
    context_bbox_size=224,
    logs_file_path="/media/data/vv/tasks/2022_12_05_debug_stable_diffusion2/log.log",

    coco_ann_path = "/media/data/vv/inference_results/Predictions_yolov5m_2022-12-06-21-38-19_dataset_heads_gunsan_yolo_3x736x736_a6fb1d1a_conf-th_0-45_2022-12-07-12-13-51/detections_coco.json", #None,
    coco_bbox_padding=20,

    inpaint_box_size = 150, 
    number_of_areas_per_image=1,


    config_path = "/app/configs/stable-diffusion/v2-inpainting-inference.yaml",
    weights_path = "/app/weights/512-inpainting-ema.ckpt",

    half_model=False,
    num_inference_steps=60,
    tags_txt_path=None, 
    number_of_tags_per_prompt=1


    return args.parse_args()


if __name__ == "__main__":
    # args = parce_args()
    
    main(
        input_images_dir="/media/data/vv/tasks/2022_12_06_test_inpainting/samples",
        result_images_dir="/media/data/vv/tasks/2022_12_05_debug_stable_diffusion2/generated/test3",
        generation_limit=10,

        base_prompt="head", 
        context_bbox_size=224,
        logs_file_path="/media/data/vv/tasks/2022_12_05_debug_stable_diffusion2/log.log",

        coco_ann_path = "/media/data/vv/inference_results/Predictions_yolov5m_2022-12-06-21-38-19_dataset_heads_gunsan_yolo_3x736x736_a6fb1d1a_conf-th_0-45_2022-12-07-12-13-51/detections_coco.json", #None,
        coco_bbox_padding=20,

        inpaint_box_size = 150, 
        number_of_areas_per_image=1,


        config_path = "/app/configs/stable-diffusion/v2-inpainting-inference.yaml",
        weights_path = "/app/weights/512-inpainting-ema.ckpt",

        half_model=False,
        num_inference_steps=60,
        tags_txt_path=None, 
        number_of_tags_per_prompt=1
    )