import torch
import numpy as np
import cv2
from torch import Tensor
from numpy import ndarray
from skimage.util import random_noise


def _pick_most_salient_pixel(
    saliency_map: ndarray, lam: float
) -> tuple[int, int, int, int]:
    h, w = saliency_map.shape
    cut_ratio = np.sqrt(1.0 - lam)
    cut_h = int(h * cut_ratio)
    cut_w = int(w * cut_ratio)

    row, col = np.unravel_index(np.argmax(saliency_map), saliency_map.shape)
    bbr1 = int(np.clip(row - cut_h // 2, 0, h))
    bbc1 = int(np.clip(col - cut_w // 2, 0, w))
    bbr2 = int(np.clip(row + cut_h // 2, 0, h))
    bbc2 = int(np.clip(col + cut_w // 2, 0, w))

    return bbr1, bbc1, bbr2, bbc2


class SaliencyMix:
    """SaliencyMix implementation from the authors.
    https://github.com/afm-shahab-uddin/SaliencyMix/
    """

    def __init__(self, beta: float) -> None:
        self.beta = beta

    @torch.no_grad()
    def __call__(self, images: Tensor, labels: Tensor) -> tuple[Tensor, Tensor]:
        # Generate mixed sample
        lam = np.random.beta(self.beta, self.beta)
        rand_index = torch.randperm(images.shape[0])
        tar_a = labels
        tar_b = labels[rand_index]
        bbx1, bby1, bbx2, bby2 = self._saliency_bbox(images[rand_index[0]], lam)
        images[:, :, bbx1:bbx2, bby1:bby2] = images[rand_index, :, bbx1:bbx2, bby1:bby2]
        # Adjust lambda to exactly match pixel ratio
        lam = 1 - (
            (bbx2 - bbx1) * (bby2 - bby1) / (images.shape[-1] * images.shape[-2])
        )
        # Compute output
        inp_var = torch.autograd.Variable(images, requires_grad=True)
        labels = tar_a * lam + tar_b * (1 - lam)
        return inp_var, labels

    def _saliency_bbox(self, image: Tensor, lam: float) -> tuple[int, int, int, int]:
        size = image.size()
        W = size[1]
        H = size[2]
        cut_rat = np.sqrt(1.0 - lam)
        cut_w = int(W * cut_rat)
        cut_h = int(H * cut_rat)

        # Initialize OpenCV's static fine grained saliency detector
        # and compute the saliency map.
        temp_img = image.cpu().numpy().transpose(1, 2, 0)
        saliency = cv2.saliency.StaticSaliencyFineGrained_create()
        (success, saliencyMap) = saliency.computeSaliency(temp_img)
        saliencyMap = (saliencyMap * 255).astype("uint8")

        maximum_indices = np.unravel_index(
            np.argmax(saliencyMap, axis=None), saliencyMap.shape
        )
        x = maximum_indices[0]
        y = maximum_indices[1]

        bbx1 = int(np.clip(x - cut_w // 2, 0, W))
        bby1 = int(np.clip(y - cut_h // 2, 0, H))
        bbx2 = int(np.clip(x + cut_w // 2, 0, W))
        bby2 = int(np.clip(y + cut_h // 2, 0, H))

        return bbx1, bby1, bbx2, bby2


class SaliencyMixFixed:
    """Fixed SaliencyMix implementation,
    which fixes wrong implementation of the authors."""

    def __init__(self, beta: float) -> None:
        self.beta = beta

    @torch.no_grad()
    def __call__(
        self, images: Tensor, labels: Tensor, sal_maps: ndarray
    ) -> tuple[Tensor, Tensor]:
        num_items = images.shape[0]
        copy_indices = np.random.permutation(num_items)
        new_images = torch.zeros_like(images)
        new_labels = torch.zeros_like(labels)

        for paste_idx, copy_idx in enumerate(copy_indices):
            lam = np.random.beta(self.beta, self.beta)
            r1, c1, r2, c2 = _pick_most_salient_pixel(sal_maps[copy_idx], lam)

            # Adjust lambda to exactly match pixel ratio.
            copy_area = (r2 - r1) * (c2 - c1)
            total_area = images.shape[-1] * images.shape[-2]
            lam = 1 - copy_area / total_area

            new_images[paste_idx, ...] = images[paste_idx, ...]
            new_images[paste_idx, :, r1:r2, c1:c2] = images[copy_idx, :, r1:r2, c1:c2]
            new_labels[paste_idx, :] = labels[paste_idx, :] * lam
            new_labels[paste_idx, :] += labels[copy_idx, :] * (1 - lam)

        return new_images, new_labels


class ErrorMix:
    """Mixes two images using SaliencyMix based on error matrix."""

    def __init__(self, beta: float, num_classes: int, exp_weight: float = 1.0) -> None:
        self.beta = beta
        self.num_classes = num_classes
        self.exp_weight = exp_weight
        self.error_matrix = np.full(
            (num_classes, num_classes), 1 / num_classes, dtype=np.float64
        )
        self.eps = 10**-12

    def __call__(
        self, images: Tensor, labels: Tensor, sal_maps: ndarray
    ) -> tuple[Tensor, Tensor]:
        num_items = images.shape[0]
        new_images = torch.zeros_like(images)
        new_labels = torch.zeros_like(labels)
        labels_idx = labels.argmax(1)

        for paste_idx in range(num_items):
            # Pick an index to be copied.
            prob = 1 - self.error_matrix[labels_idx[paste_idx], labels_idx]
            prob += self.eps
            prob[paste_idx] = 0
            prob /= prob.sum()
            copy_idx = np.random.choice(num_items, p=prob)

            lam = np.random.beta(self.beta, self.beta)
            r1, c1, r2, c2 = _pick_most_salient_pixel(sal_maps[copy_idx], lam)

            # Adjust lambda to exactly match pixel ratio.
            copy_area = (r2 - r1) * (c2 - c1)
            total_area = images.shape[-1] * images.shape[-2]
            lam = 1 - copy_area / total_area

            new_images[paste_idx, ...] = images[paste_idx, ...]
            new_images[paste_idx, :, r1:r2, c1:c2] = images[copy_idx, :, r1:r2, c1:c2]
            new_labels[paste_idx, :] = labels[paste_idx, :] * lam
            new_labels[paste_idx, :] += labels[copy_idx, :] * (1 - lam)

        return new_images, new_labels

    @torch.no_grad()
    def update_error_matrix(self, outputs: Tensor, labels: Tensor) -> None:
        labels_arr = labels.cpu().numpy()  # Assuming already normalized
        outputs_arr = outputs.cpu().numpy()  # Needs to be normalized
        outputs_arr -= outputs_arr.min(1, keepdims=True)
        outputs_arr /= outputs_arr.sum(1, keepdims=True)
        diff_matrix = np.absolute(outputs_arr - labels_arr)

        num_items = outputs_arr.shape[0]
        for i in range(num_items):
            label_index = labels_arr[i].argmax()
            self.error_matrix[label_index, :] = (
                self.exp_weight * diff_matrix[i, :]
                + (1 - self.exp_weight) * self.error_matrix[label_index, :]
            )


class LocalMeanSaliencyMix:
    """SaliencyMix implementation from the authors.
    https://github.com/afm-shahab-uddin/SaliencyMix/
    """

    def __init__(self, beta: float) -> None:
        self.beta = beta

    @torch.no_grad()
    def __call__(self, images: Tensor, labels: Tensor) -> tuple[Tensor, Tensor]:
        # Generate mixed sample
        lam = np.random.beta(self.beta, self.beta)
        rand_index = torch.randperm(images.shape[0])
        tar_a = labels
        tar_b = labels[rand_index]
        bbx1, bby1, bbx2, bby2 = self._saliency_bbox(images[rand_index[0]], lam)
        images[:, :, bbx1:bbx2, bby1:bby2] = images[rand_index, :, bbx1:bbx2, bby1:bby2]
        # Adjust lambda to exactly match pixel ratio
        lam = 1 - (
            (bbx2 - bbx1) * (bby2 - bby1) / (images.shape[-1] * images.shape[-2])
        )
        # Compute output
        inp_var = torch.autograd.Variable(images, requires_grad=True)
        labels = tar_a * lam + tar_b * (1 - lam)
        return inp_var, labels

    def _saliency_bbox(self, image: Tensor, lam: float) -> tuple[int, int, int, int]:
        size = image.size()
        W = size[1]
        H = size[2]
        cut_rat = np.sqrt(1.0 - lam)
        cut_w = int(W * cut_rat)
        cut_h = int(H * cut_rat)

        # Initialize OpenCV's static fine grained saliency detector
        # and compute the saliency map.
        temp_img = image.cpu().numpy().transpose(1, 2, 0)
        saliency = cv2.saliency.StaticSaliencyFineGrained_create()
        (success, saliencyMap) = saliency.computeSaliency(temp_img)
        saliencyMap = (saliencyMap * 255).astype("uint8")

        saliency_map_ = saliencyMap.copy()
        saliency_map_[: cut_w // 2, :] = False
        saliency_map_[:, : cut_h // 2] = False
        saliency_map_[W - cut_w // 2 :, :] = False
        saliency_map_[:, H - cut_h // 2 :] = False

        x_y_indices = np.where(saliency_map_)

        mean_values = []
        bboxes = []
        for i in range(10):
            x_index = np.random.choice(len(x_y_indices[0]))
            y_index = np.random.choice(len(x_y_indices[1]))
            random_indices = (x_y_indices[0][x_index], x_y_indices[1][y_index])
            x = random_indices[0]
            y = random_indices[1]

            bbx1 = int(np.clip(x - cut_w // 2, 0, W))
            bby1 = int(np.clip(y - cut_h // 2, 0, H))
            bbx2 = int(np.clip(x + cut_w // 2, 0, W))
            bby2 = int(np.clip(y + cut_h // 2, 0, H))

            bboxes.append([bbx1, bbx2, bby1, bby2])

            mean_values.append(saliencyMap[bbx1:bbx2, bby1:bby2].mean())

        best = np.argmax(mean_values)
        f_bbx1, f_bbx2, f_bby1, f_bby2 = bboxes[best]

        return f_bbx1, f_bby1, f_bbx2, f_bby2
    
    
    
class LocalMeanSaliencyMixFixed:
    """SaliencyMix implementation from the authors.
    https://github.com/afm-shahab-uddin/SaliencyMix/
    """

    def __init__(self, beta: float) -> None:
        self.beta = beta

    @torch.no_grad()
    def __call__(self, images: Tensor, labels: Tensor) -> tuple[Tensor, Tensor]:
        # Generate mixed sample
        num_items = images.shape[0]

        for paste_idx in range(num_items):
            copy_idx = np.random.randint(num_items)
            lam = np.random.beta(self.beta, self.beta)
            r1, c1, r2, c2 = self._saliency_bbox(images[copy_idx], lam)
            images[paste_idx, :, r1:r2, c1:c2] = images[copy_idx, :, r1:r2, c1:c2]

            # Adjust lambda to exactly match pixel ratio
            copy_area = (r2 - r1) * (c2 - c1)
            total_area = images.shape[-1] * images.shape[-2]
            lam = 1 - copy_area / total_area
            
            labels[paste_idx] = labels[paste_idx] * lam + labels[copy_idx] * (1 - lam)

        return images, labels

    def _saliency_bbox(self, image: Tensor, lam: float) -> tuple[int, int, int, int]:
        size = image.size()
        W = size[1]
        H = size[2]
        cut_rat = np.sqrt(1.0 - lam)
        cut_w = int(W * cut_rat)
        cut_h = int(H * cut_rat)

        # Initialize OpenCV's static fine grained saliency detector
        # and compute the saliency map.
        temp_img = image.cpu().numpy().transpose(1, 2, 0)
        saliency = cv2.saliency.StaticSaliencyFineGrained_create()
        (success, saliencyMap) = saliency.computeSaliency(temp_img)
        saliencyMap = (saliencyMap * 255).astype("uint8")

        saliency_map_ = saliencyMap.copy()
        saliency_map_[: cut_w // 2, :] = False
        saliency_map_[:, : cut_h // 2] = False
        saliency_map_[W - cut_w // 2 :, :] = False
        saliency_map_[:, H - cut_h // 2 :] = False

        x_y_indices = np.where(saliency_map_)

        mean_values = []
        bboxes = []
        for i in range(10):
            x_index = np.random.choice(len(x_y_indices[0]))
            y_index = np.random.choice(len(x_y_indices[1]))
            random_indices = (x_y_indices[0][x_index], x_y_indices[1][y_index])
            x = random_indices[0]
            y = random_indices[1]

            bbx1 = int(np.clip(x - cut_w // 2, 0, W))
            bby1 = int(np.clip(y - cut_h // 2, 0, H))
            bbx2 = int(np.clip(x + cut_w // 2, 0, W))
            bby2 = int(np.clip(y + cut_h // 2, 0, H))

            bboxes.append([bbx1, bbx2, bby1, bby2])

            mean_values.append(saliencyMap[bbx1:bbx2, bby1:bby2].mean())

        best = np.argmax(mean_values)
        f_bbx1, f_bbx2, f_bby1, f_bby2 = bboxes[best]

        return f_bbx1, f_bby1, f_bbx2, f_bby2


class NoiseSaliencyMix:
    def __init__(self, beta: float, std_dev=0.2) -> None:
        self.beta = beta
        self.std_dev = std_dev

    @torch.no_grad()
    def __call__(
        self, images: Tensor, labels: Tensor, sal_maps: ndarray
    ) -> tuple[Tensor, Tensor]:
        # Generate mixed sample
        lam = np.random.beta(self.beta, self.beta)
        rand_index = torch.randperm(images.shape[0])
        tar_a = labels
        tar_b = labels[rand_index]
        r1, c1, r2, c2 = _pick_most_salient_pixel(sal_maps[rand_index[0]], lam)

        # gaussian noise patch
        patch = images[rand_index, :, r1:r2, c1:c2]
        if patch.numel() != 0:
            gaussian_patch = random_noise(
                patch, mode="gaussian", var=self.std_dev**2, clip=False
            )
            images[:, :, r1:r2, c1:c2] = torch.tensor(gaussian_patch)
        else:
            images[:, :, r1:r2, c1:c2] = images[rand_index, :, r1:r2, c1:c2]

        # Adjust lambda to exactly match pixel ratio
        copy_area = (r2 - r1) * (c2 - c1)
        total_area = images.shape[-1] * images.shape[-2]
        lam = 1 - (copy_area / total_area)

        # Compute output
        labels = tar_a * lam + tar_b * (1 - lam)
        return images, labels
    
    
class NoiseSaliencyMixFixed:
    def __init__(self, beta: float, std_dev=0.2) -> None:
        self.beta = beta
        self.std_dev = std_dev

    @torch.no_grad()
    def __call__(
        self, images: Tensor, labels: Tensor, sal_maps: ndarray
    ) -> tuple[Tensor, Tensor]:
        num_items = images.shape[0]

        for paste_idx in range(num_items):
            copy_idx = np.random.randint(num_items)
            lam = np.random.beta(self.beta, self.beta)
            r1, c1, r2, c2 = _pick_most_salient_pixel(sal_maps[copy_idx], lam)

        # gaussian noise patch
        patch = images[copy_idx, :, r1:r2, c1:c2]
        if patch.numel() != 0:
            gaussian_patch = random_noise(
                patch, mode="gaussian", var=self.std_dev**2, clip=False
            )
            images[paste_idx, :, r1:r2, c1:c2] = torch.tensor(gaussian_patch)
        else:
            images[paste_idx, :, r1:r2, c1:c2] = images[copy_idx, :, r1:r2, c1:c2]

        # Adjust lambda to exactly match pixel ratio
        copy_area = (r2 - r1) * (c2 - c1)
        total_area = (images.shape[-1] * images.shape[-2])
        lam = 1 - (copy_area / total_area)

        # Compute output
        labels[paste_idx] = labels[paste_idx] * lam + labels[copy_idx] * (1 - lam)
        
        return images, labels
    
    

class randsalMix:

    def __init__(self, beta: float) -> None:
        self.beta = beta

    def __call__(self, images: Tensor, labels: Tensor, sal_maps):
        num_items = images.shape[0]
        temp_labels = labels
        
        for paste_idx in range(num_items):
            copy_idx = np.random.randint(num_items)
            lam = np.random.beta(self.beta, self.beta)
            
            val = 0
            cx1, cy1, cx2, cy2, val = self.randsal_bbox(sal_maps[copy_idx], lam, val)
            px1, py1, px2, py2, val =  self.randsal_bbox(sal_maps[paste_idx], lam, val)

            if val == -1:
                continue

            images[paste_idx, :, px1:px2, py1:py2] = images[copy_idx, :, cx1:cx2, cy1:cy2]            
            copy_area = (cx2 - cx1) * (cy2 - cy1)
            total_area = images.shape[-1] * images.shape[-2]
            lam = 1 - copy_area / total_area
            labels[paste_idx] = temp_labels[paste_idx] * lam + temp_labels[copy_idx] * (1 - lam)

        return images, labels


    def randsal_bbox(self, saliency_map, lam, val):
        size = saliency_map.size()
        cut_rat = np.sqrt(1. - lam)
        cut_w = int(size[0] * cut_rat)
        cut_h = int(size[1] * cut_rat)

        saliency_map[:cut_w//2, :] = 0.0
        saliency_map[-cut_w//2:, :] = 0.0
        saliency_map[:, :cut_h//2] = 0.0
        saliency_map[:, -cut_h//2:] = 0.0
        saliency_map_1d = np.array(saliency_map).flatten()

        s_nonzero = saliency_map_1d[saliency_map_1d != 0]
        s_sum = np.sum(s_nonzero)
        prob = np.zeros_like(saliency_map_1d, dtype=float)
        #copy
        if val == 0:
            if s_sum != 0:
                prob[saliency_map_1d != 0] = s_nonzero / s_sum
            val = 1
        #paste
        elif val == 1:
            if s_sum != 0:
                prob[saliency_map_1d != 0] = (255 - s_nonzero) / (255 - s_nonzero).sum()
            
        if prob.sum() == 1:
            pick = np.random.choice(len(saliency_map_1d), p=prob)
            pick_idx = np.unravel_index(pick, saliency_map.shape)
            cx, cy = int(pick_idx[0]), int(pick_idx[1])
            bbx1 = cx - cut_w // 2
            bby1 = cy - cut_h // 2
            bbx2 = cx + cut_w // 2
            bby2 = cy + cut_h // 2

            return bbx1, bby1, bbx2, bby2, val
      
        else:
            return -1, -1, -1, -1, -1
