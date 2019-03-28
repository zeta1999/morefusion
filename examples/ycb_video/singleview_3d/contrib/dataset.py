import imgviz
import numpy as np
import trimesh.transformations as tf

import objslampp


class Dataset(objslampp.datasets.base.DatasetBase):

    _root_dir = objslampp.datasets.YCBVideoDataset._root_dir

    voxel_dim = 32

    def __init__(self, split, class_ids=None):
        super().__init__()
        assert split in ('train', 'val')
        self._split = split
        self._class_ids = class_ids
        self._ids = self._get_ids()

        self._cache_pitch = {}

    def _get_ids(self):
        if self.split == 'val':
            ids = objslampp.datasets.YCBVideoDataset(
                split='keyframe'
            ).get_ids()
        else:
            assert self.split == 'train'
            ids = objslampp.datasets.YCBVideoDataset(
                split='train'
            ).get_ids(sampling=8)

        ids = [(True, x) for x in ids]

        if self.split == 'train':
            ids_syn = objslampp.datasets.YCBVideoSyntheticDataset().get_ids()
            ids_syn = [(False, x) for x in ids_syn]
            ids += ids_syn

        return tuple(ids)

    def _get_invalid_data(self):
        return dict(
            class_id=-1,
            pitch=0.,
            rgb=np.zeros((256, 256, 3), dtype=np.uint8),
            pcd=np.zeros((256, 256, 3), dtype=np.float64),
            quaternion_true=np.zeros((4,), dtype=np.float64),
            translation_true=np.zeros((3,), dtype=np.float64),
        )

    def _get_pitch(self, class_id):
        if class_id in self._cache_pitch:
            return self._cache_pitch[class_id]

        models = objslampp.datasets.YCBVideoModels()
        cad_file = models.get_model(class_id=class_id)['textured_simple']
        bbox_diagonal = models.get_bbox_diagonal(mesh_file=cad_file)
        pitch = 1. * bbox_diagonal / self.voxel_dim
        pitch = pitch.astype(np.float32)

        self._cache_pitch[class_id] = pitch
        return pitch

    def get_example(self, index):
        is_real, image_id = self._ids[index]

        if is_real:
            frame = objslampp.datasets.YCBVideoDataset.get_frame(image_id)
        else:
            frame = objslampp.datasets.YCBVideoSyntheticDataset.get_frame(
                image_id
            )

        class_ids = frame['meta']['cls_indexes']

        if self._class_ids is None:
            class_id = np.random.choice(class_ids)
        elif not any(c in class_ids for c in self._class_ids):
            return self._get_invalid_data()
        else:
            class_id = np.random.choice(self._class_ids)

        instance_id = np.where(class_ids == class_id)[0][0]
        T_cad2cam = frame['meta']['poses'][:, :, instance_id]
        quaternion_true = tf.quaternion_from_matrix(T_cad2cam)
        translation_true = tf.translation_from_matrix(T_cad2cam)

        mask = frame['label'] == class_id
        if mask.sum() == 0:
            return self._get_invalid_data()

        bbox = objslampp.geometry.masks_to_bboxes([mask])[0]
        y1, x1, y2, x2 = bbox.round().astype(int)
        if (y2 - y1) * (x2 - x1) == 0:
            return self._get_invalid_data()

        rgb = frame['color'].copy()
        rgb[~mask] = 0
        rgb = rgb[y1:y2, x1:x2]
        rgb = imgviz.centerize(rgb, (256, 256))

        depth = frame['depth']
        K = frame['meta']['intrinsic_matrix']
        pcd = objslampp.geometry.pointcloud_from_depth(
            depth, fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2],
        )
        pcd[~mask] = np.nan
        pcd = pcd[y1:y2, x1:x2]
        pcd = imgviz.centerize(pcd, (256, 256), cval=np.nan)
        if np.isnan(pcd).any(axis=2).all():
            return self._get_invalid_data()

        return dict(
            class_id=class_id,
            pitch=self._get_pitch(class_id=class_id),
            rgb=rgb,
            pcd=pcd,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
        )