import cv2
import os
import yaml
import tqdm
import argparse
import torch
import numpy as np
import torchvision.transforms as transforms
import matplotlib
import matplotlib.pyplot as plt
import PIL.Image as pil
from easydict import EasyDict
from scipy.spatial.transform import Rotation

from tools.load import LoadMADSData
from tools.common import project_3d_to_2d, get_projection_matrix, triangulation
from tools.utils import get_max_preds
from models.poseresnet import PoseResNet

matplotlib.use("Agg")


def project(meta):
    K_left = meta['cam_left']['intrinsics']
    R_left = meta['cam_left']['rotation']
    T_left = meta['cam_left']['translation']

    K_right = meta['cam_right']['intrinsics']
    R_right = meta['cam_right']['rotation']
    T_right = meta['cam_right']['translation']

    pose_3d = np.array(meta['pose_3d'])
    pose_2d_left = project_3d_to_2d(pose_3d, K_left, R_left, T_left)
    pose_2d_right = project_3d_to_2d(pose_3d, K_right, R_right, T_right)

    return pose_2d_left, pose_2d_right


def plot_body(ax, points, color, label):
    # Define the connections between the joints
    connections = [
        (0, 1),   # body
        (0, 18),  # head
        (1, 6), (6, 7), (7, 8), (8, 9),  # left leg
        (0, 14), (14, 15), (15, 16), (16, 17),  # left arm
        (1, 2), (2, 3), (3, 4), (4, 5),  # right leg
        (0, 10), (10, 11), (11, 12), (12, 13),  # right arm
    ]

    # Plot the skeleton joints
    ax.scatter(points[:, 0], points[:, 1], points[:, 2],
               c=color, marker='o', s=2)

    for connection in connections:
        joint1 = points[connection[0]]
        joint2 = points[connection[1]]
        ax.plot([joint1[0], joint2[0]],
                [joint1[1], joint2[1]],
                [joint1[2], joint2[2]], c=color)

    ax.plot([], [], c=color, label=label)


def plot_pose_3d(pose_3d, pts3D):
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlim3d(-600, 600)
    ax.set_ylim3d(-1000, 200)
    ax.set_zlim3d(0, 1500)

    rot = Rotation.from_euler('zyx', np.array([0, 0, 90]),
                              degrees=True).as_matrix()
    pose_3d = (rot @ pose_3d.T).T
    pts3D = (rot @ pts3D.T).T

    plot_body(ax, pose_3d, '#03459c', "ground truth")
    plot_body(ax, pts3D, '#27d128', "estimation")

    # Set labels and title
    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')
    ax.set_title('3D Human Skeleton')
    ax.legend()

    # Convert the plot to numpy array
    canvas = fig.canvas
    canvas.draw()
    width, height = canvas.get_width_height()
    image_array = np.frombuffer(canvas.tostring_rgb(), dtype='uint8')
    image_array = image_array.reshape(height, width, 3)
    plt.close()

    return image_array


def plot_pose_2d(gt_joints, pred_joints, imgs):
    def plot_joints(joints, img, color):
        for k in range(joints.shape[0]):
            joint = joints[k]
            cv2.circle(img, (int(joint[0]), int(joint[1])),
                       2, color, -1)

    for gt, pred, img in zip(gt_joints, pred_joints, imgs):
        plot_joints(gt, img, (255, 0, 0))
        plot_joints(pred, img, (0, 255, 0))

    img = np.concatenate(imgs, axis=1)

    return img


class BaseLine:
    def __init__(self, config):
        device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

        model = PoseResNet(config)
        self.model = model.to(device)

        # Load the model weights
        weight_path = os.path.join("weights", config.MODEL.NAME, "best.pth")
        if os.path.exists(weight_path):
            model.load_state_dict(torch.load(weight_path, map_location=device))
        else:
            assert False, "Model is not exist in {}".format(weight_path)

        self.model.eval()
        self.device = device

        self.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ])

    def inference(self, inputs):
        inputs = self.transform(inputs).unsqueeze(0)
        inputs = inputs.to(self.device)
        outputs = self.model(inputs)

        # Get keypoints locations from heatmap
        preds, _ = get_max_preds(outputs.detach().cpu().numpy())
        preds = preds * 4.0
        preds = preds.astype(np.uint8)

        return preds

    def estimate(self, img_left, img_right, meta):
        pose_2d_left, pose_2d_right = project(meta)

        preds_left = self.inference(img_left.copy()).squeeze(0)
        preds_right = self.inference(img_right.copy()).squeeze(0)

        img_2d = plot_pose_2d((pose_2d_left, pose_2d_right),
                              (preds_left, preds_right),
                              (img_left, img_right))
        img_2d = cv2.cvtColor(img_2d, cv2.COLOR_BGR2RGB)

        PL = get_projection_matrix(meta['cam_left']['intrinsics'],
                                   meta['cam_left']['rotation'],
                                   meta['cam_left']['translation'])
        PR = get_projection_matrix(meta['cam_right']['intrinsics'],
                                   meta['cam_right']['rotation'],
                                   meta['cam_right']['translation'])

        pts3D = triangulation(PL, PR, preds_left, preds_right)
        img_3d = plot_pose_3d(np.array(meta['pose_3d']), pts3D)

        ratio = img_2d.shape[1] / img_3d.shape[1]
        img_3d = cv2.resize(
            img_3d,
            (int(img_3d.shape[1] * ratio), int(img_3d.shape[0] * ratio))
        )

        img = np.vstack((img_2d, img_3d))
        return img


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config_path", type=str,
                        default="configs/mads_2d.yaml",
                        help="Path to the config file")
    args = parser.parse_args()

    with open(args.config_path, 'r') as f:
        config = EasyDict(yaml.safe_load(f))

    MADS_loader = LoadMADSData("data/MADS_extract/valid",
                               config.MODEL.IMAGE_SIZE)

    method = BaseLine(config)

    images = []
    for img_left, img_right, meta in tqdm.tqdm(MADS_loader,
                                               total=len(MADS_loader)):
        pose_img = method.estimate(img_left, img_right, meta)

        im = pil.fromarray(pose_img)
        images.append(im)

    images[0].save('pose.gif',
                   save_all=True, append_images=images[1:],
                   optimize=False, duration=40, loop=0)
