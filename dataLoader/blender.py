import cv2
import jittor as jt
from jittor.dataset.dataset import Dataset
import json
from numpy import dtype
from tqdm import tqdm
import os
from PIL import Image
import numpy as np
import jittor.transform as T

from .ray_utils import *
jt.flags.use_cuda = 1

class BlenderDataset(Dataset):
    def __init__(self, datadir, split='train', downsample=1.0, is_stack=False, N_vis=-1, scene_box=[-30., -30., -30., 30., 30., 30.], near_far=[0.2,30]):

        self.N_vis = N_vis
        self.root_dir = datadir
        self.split = split
        self.is_stack = is_stack
        self.img_wh = (int(800/downsample),int(800/downsample))
        self.define_transforms()

        self.scene_bbox = jt.array(scene_box).reshape(2, 3)
        self.blender2opencv = np.array([[-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.read_meta()
        self.define_proj_mat()

        self.white_bg = True
        self.near_far = near_far
        
        self.center = jt.mean(self.scene_bbox, 0).float().view(1, 1, 3)
        self.radius = (self.scene_bbox[1] - self.center).float().view(1, 1, 3)
        self.downsample=downsample

    def read_depth(self, filename):
        depth = np.array(read_pfm(filename)[0], dtype=np.float32)  # (800, 800)
        return depth
    
    def read_meta(self):

        with open(os.path.join(self.root_dir, f"transforms_{self.split}.json"), 'r') as f:
            self.meta = json.load(f)

        w, h = self.img_wh
        self.focal = 0.5 * 800 / np.tan(0.5 * self.meta['camera_angle_x'])  # original focal length
        self.focal *= self.img_wh[0] / 800  # modify focal length to match size self.img_wh


        # ray directions for all pixels, same for all images (same H, W, focal)
        self.directions = get_ray_directions(h, w, [self.focal,self.focal])  # (h, w, 3)
        #self.directions = self.directions / jt.norm(self.directions, dim=-1, keepdim=True)
        self.intrinsics = jt.float64([[self.focal,0,w/2],[0,self.focal,h/2],[0,0,1]]).float32()

        self.image_paths = []
        self.poses = []
        self.all_rays = []
        self.all_rgbs = []
        self.all_masks = []
        self.all_depth = []
        self.downsample=1.0

        img_eval_interval = 1 if self.N_vis < 0 else len(self.meta['frames']) // self.N_vis
        idxs = list(range(0, len(self.meta['frames']), img_eval_interval))
        for i in tqdm(idxs, desc=f'Loading data {self.split} ({len(idxs)})'):#img_list:#

            frame = self.meta['frames'][i]
            pose = np.array(frame['transform_matrix']) @ self.blender2opencv
            c2w = jt.float32(pose)
            self.poses += [c2w]

            image_path = os.path.join(self.root_dir, f"{frame['file_path']}.png")
            self.image_paths += [image_path]
            img = Image.open(image_path)
            
            if self.downsample!=1.0:
                img = img.resize(self.img_wh, Image.LANCZOS)
            img = jt.float32(self.transform(img))  # (4, h, w)
            img = img.view(4, -1).permute(1, 0)  # (h*w, 4) RGBA
            img = img[:, :3] * img[:, -1:] + (1 - img[:, -1:])  # blend A to RGB
            self.all_rgbs += [img]


            rays_o, rays_d, dx = get_rays(self.directions, c2w)  # both (h*w, 3)
            self.all_rays += [jt.concat([rays_o, rays_d, dx], 1)]  # (h*w, 9)


        self.poses = jt.stack(self.poses)
        if not self.is_stack:
            self.all_rays = jt.concat(self.all_rays, 0)  # (len(self.meta['frames])*h*w, 3)
            self.all_rgbs = jt.concat(self.all_rgbs, 0)  # (len(self.meta['frames])*h*w, 3)

#             self.all_depth = jt.concat(self.all_depth, 0)  # (len(self.meta['frames])*h*w, 3)
        else:
            self.all_rays = jt.stack(self.all_rays, 0)  # (len(self.meta['frames]),h*w, 3)
            self.all_rgbs = jt.stack(self.all_rgbs, 0).reshape(-1,*self.img_wh[::-1], 3)  # (len(self.meta['frames]),h,w,3)
            # self.all_masks = jt.stack(self.all_masks, 0).reshape(-1,*self.img_wh[::-1])  # (len(self.meta['frames]),h,w,3)

    def define_transforms(self):
        self.transform = T.ToTensor()

        
    def define_proj_mat(self):
        # TODO:直接用jittor进行计算
        test1=self.intrinsics.unsqueeze(0).numpy()
        test2=self.poses.numpy()
        test2=np.linalg.inv(test2)
        test2=test2[:,:3]
        ans=test1 @ test2
        self.proj_mat = jt.array(ans)

    def world2ndc(self,points,lindisp=None):

        return (points - self.center) / self.radius
        
    def __len__(self):
        return len(self.all_rgbs)

    def __getitem__(self, idx):

        if self.split == 'train':  # use data in the buffers
            sample = {'rays': self.all_rays[idx],
                      'rgbs': self.all_rgbs[idx]}

        else:  # create data for each image separately

            img = self.all_rgbs[idx]
            rays = self.all_rays[idx]
            mask = self.all_masks[idx] # for quantity evaluation

            sample = {'rays': rays,
                      'rgbs': img,
                      'mask': mask}
        return sample
