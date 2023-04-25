import glob
import os
import time
import tqdm
import warnings

import torch
import imageio
import numpy as np
import taichi as ti
from einops import rearrange
import torch.nn.functional as F
from kornia.utils.grid import create_meshgrid3d

from gui import NGPGUI
from opt import get_opts
from datasets import dataset_dict
from datasets.ray_utils import get_rays

from modules.losses import NeRFLoss
from modules.networks import TaichiNGP
from modules.rendering import MAX_SAMPLES, render
from modules.utils import load_ckpt, depth2img

from torchmetrics import (
    PeakSignalNoiseRatio, StructuralSimilarityIndexMeasure
)
from torch.optim.lr_scheduler import CosineAnnealingLR

warnings.filterwarnings("ignore")

def taichi_init(args):
    taichi_init_args = {"arch": ti.cuda, "device_memory_GB": 4.0}
    if args.half2_opt:
        taichi_init_args["half2_vectorization"] = True

    ti.init(**taichi_init_args)


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    hparams = get_opts()
    taichi_init(hparams)

    val_dir = 'results/'

    # rendering configuration
    random_background = hparams.random_bg
    exp_step_factor = 1 / 256 if hparams.scale > 0.5 else 0.

    # occupancy grid update configuration
    warmup_steps = 256
    update_interval = 16

    # datasets
    dataset = dataset_dict[hparams.dataset_name]
    train_dataset = dataset(
        root_dir=hparams.root_dir,
        split=hparams.split,
        downsample=hparams.downsample,
    ).to(device)
    train_dataset.batch_size = hparams.batch_size
    train_dataset.ray_sampling_strategy = hparams.ray_sampling_strategy

    test_dataset = dataset(
        root_dir=hparams.root_dir,
        split='test',
        downsample=hparams.downsample,
    ).to(device)
    # TODO: add test set rendering code


    # loss 
    nerf_loss = NeRFLoss(
        lambda_distortion=hparams.distortion_loss_w
    ).to(device)

    # metric
    val_psnr = PeakSignalNoiseRatio(
        data_range=1
    ).to(device)
    val_ssim = StructuralSimilarityIndexMeasure(
        data_range=1
    ).to(device)

    # model
    model = TaichiNGP(hparams, scale=hparams.scale)
    model.register_buffer(
        'density_grid',
        torch.zeros(
            model.cascades, model.grid_size**3)
        
    )
    model.register_buffer(
        'grid_coords',
        create_meshgrid3d(
            model.grid_size, 
            model.grid_size, 
            model.grid_size, 
            False,
            dtype=torch.int32
        ).reshape(-1, 3)
    )
    model = model.to(device)
    if hparams.ckpt_path:
        load_ckpt(model, hparams.ckpt_path)
        print("Load checkpoint from %s" % hparams.ckpt_path)

    model.mark_invisible_cells(
        train_dataset.K,
        train_dataset.poses, 
        train_dataset.img_wh,
    )

    grad_scaler = torch.cuda.amp.GradScaler()
    # optimizer
    try:
        import apex
        optimizer = apex.optimizers.FusedAdam(
            model.parameters(), 
            lr=hparams.lr, 
            eps=1e-15,
        )
    except ImportError:
        print("Failed to import apex FusedAdam, use torch Adam instead.")
        optimizer = torch.optim.Adam(
            model.parameters(), 
            hparams.lr, 
            eps=1e-15,
        )

    # scheduler
    scheduler = CosineAnnealingLR(
        optimizer, 
        hparams.max_steps // hparams.step_per_epoch,
        hparams.lr / 30
    )

    # training loop
    tic = time.time()
    for step in range(hparams.max_steps):
        model.train()

        i = torch.randint(0, len(train_dataset), (1,)).item()
        data = train_dataset[i]

        direction = data['direction']
        pose = data['pose']

        with torch.autocast(device_type='cuda', dtype=torch.float16):
            if step % update_interval == 0:
                model.update_density_grid(
                    0.01 * MAX_SAMPLES / 3**0.5,
                    warmup=step < warmup_steps,
                )

            # get rays
            rays_o, rays_d = get_rays(direction, pose)
            # render image
            results = render(
                model, 
                rays_o, 
                rays_d,
                exp_step_factor=exp_step_factor,
                random_bg=random_background,
            )
            losses = nerf_loss(results, data)
            loss = sum(lo.mean() for lo in losses.values())

        optimizer.zero_grad()
        grad_scaler.scale(loss).backward()
        grad_scaler.step(optimizer)
        # scale = grad_scaler.get_scale()
        grad_scaler.update()

        if step % hparams.step_per_epoch == 0:
            elapsed_time = time.time() - tic
            scheduler.step()

            with torch.no_grad():
                mse = F.mse_loss(results['rgb'], data['rgb'])
                psnr = -10.0 * torch.log(mse) / np.log(10.0)
            print(
                f"elapsed_time={elapsed_time:.2f}s | "
                f"step={step} | psnr={psnr:.4f} | "
                f"loss={loss:.4f} | "
                # ray marching samples per ray (occupied space on the ray)
                f"rm_s={results['rm_samples'] / len(data['rgb'])} | "
                # volume rendering samples per ray 
                # (stops marching when transmittance drops below 1e-4)
                f"vr_s={results['vr_samples'] / len(data['rgb'])} | "
            )

        if step % hparams.max_steps == 0 and step > 0:
            torch.save(
                model.state_dict(),
                os.path.join(val_dir, 'model.pth'),
            )
            # test loop
            torch.cuda.empty_cache()
            progress_bar = tqdm.tqdm(total=len(test_dataset), desc=f'evaluating: ')
            with torch.no_grad():
                model.eval()
                w, h = test_dataset.img_wh
                directions = test_dataset.directions
                test_psnrs = []
                test_ssims = []
                for test_step in range(len(test_dataset)):
                    progress_bar.update()
                    test_data = test_dataset[test_step]

                    rgb_gt = test_data['rgb']
                    poses = test_data['pose']

                    # get rays
                    rays_o, rays_d = get_rays(directions, poses)
                    # render image
                    results = render(
                        model, 
                        rays_o, 
                        rays_d,
                        test_time=True,
                        exp_step_factor=exp_step_factor,
                        random_bg=random_background,
                    )
                    rgb_pred = rearrange(results['rgb'], '(h w) c -> 1 c h w', h=h)

                    # get psnr
                    val_psnr(results['rgb'], rgb_gt)
                    test_psnrs.append(val_psnr.compute())
                    val_psnr.reset()
                    # get ssim
                    val_ssim(rgb_pred, rgb_gt)
                    test_ssims.append(val_ssim.compute())
                    val_ssim.reset()

                    # save test image to disk
                    if not hparams.no_save_test:
                        test_idx = test_data['img_idxs']
                        rgb_pred = rearrange(
                            results['rgb'].cpu().numpy(),
                            '(h w) c -> h w c',
                            h=h
                        )
                        rgb_pred = (rgb_pred * 255).astype(np.uint8)
                        depth = depth2img(
                            rearrange(results['depth'].cpu().numpy(), '(h w) -> h w', h=h))
                        imageio.imsave(
                            os.path.join(
                                val_dir, 
                                f'rgb_{test_idx:03d}.png'
                                ),
                            rgb_pred
                        )
                        imageio.imsave(
                            os.path.join(
                                val_dir, 
                                f'depth_{test_idx:03d}.png'
                            ),
                            depth
                        )

                progress_bar.close()


    if hparams.gui:
        ti.reset()
        hparams.ckpt_path = os.path.join(val_dir, 'model.pth')
        taichi_init(hparams)
        dataset = dataset_dict[hparams.dataset_name](
            root_dir=hparams.root_dir,
            downsample=hparams.downsample,
            read_meta=True,
        )
        NGPGUI(hparams, dataset.K, dataset.img_wh, dataset.poses).render()

if __name__ == '__main__':
    main()