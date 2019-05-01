#!/usr/bin/env python

import chainer
import chainer.functions as F
import glooey
import numpy as np
import octomap
import pyglet
import trimesh
import trimesh.transformations as tf

import objslampp

import contrib

from build_occupancy_grid import get_instance_grid


class OccupancyGridAlignmentModel(chainer.Link):

    def __init__(self, quaternion_init=None, translation_init=None):
        super().__init__()
        with self.init_scope():
            if quaternion_init is None:
                quaternion_init = np.array([1, 0, 0, 0], dtype=np.float32)
            self.quaternion = chainer.Parameter(
                initializer=quaternion_init
            )
            if translation_init is None:
                translation_init = np.zeros((3,), dtype=np.float32)
            self.translation = chainer.Parameter(
                initializer=translation_init
            )

    def forward(
        self,
        points_source,
        grid_target,
        id_target,
        *,
        pitch,
        origin,
        connectivity,
    ):
        transform = objslampp.functions.quaternion_matrix(
            self.quaternion[None]
        )
        transform = objslampp.functions.compose_transform(
            transform[:, :3, :3], self.translation[None]
        )

        points_source = objslampp.functions.transform_points(
            points_source, transform
        )[0]
        grid_source = objslampp.functions.occupancy_grid_3d(
            points_source,
            pitch=pitch,
            origin=origin,
            dimension=grid_target.shape,
            connectivity=connectivity,
        )

        assert grid_target.dtype == np.uint8
        occupied_target = (grid_target == id_target).astype(np.float32)
        intersection = F.sum(occupied_target * grid_source)
        denominator = F.sum(occupied_target)
        reward = intersection / denominator

        # unknown: 255
        # free: 254
        # occupied background: 0
        # occupied instance1: 1
        # ...
        # occupied by untarget or empty
        unoccupied_target = ~np.isin(grid_target, [id_target, 255])
        unoccupied_target = unoccupied_target.astype(np.float32)
        intersection = F.sum(unoccupied_target * grid_source)
        denominator = F.sum(grid_source)
        penalty = intersection / denominator

        loss = - reward + penalty
        return loss


class InstanceOccupancyGridRegistration:

    def __init__(
        self,
        points_source,
        grid_target,
        id_target,
        *,
        pitch,
        origin,
        connectivity,
    ):
        self._points_source = points_source
        self._grid_target = grid_target
        self._id_target = id_target
        self._pitch = pitch
        self._origin = origin
        self._connectivity = connectivity

        model = OccupancyGridAlignmentModel()
        self._optimizer = chainer.optimizers.Adam(alpha=0.1)
        self._optimizer.setup(model)
        model.translation.update_rule.hyperparam.alpha *= 0.1

        self._iteration = -1

    def step(self):
        self._iteration += 1

        model = self._optimizer.target

        loss = model(
            points_source=self._points_source,
            grid_target=self._grid_target,
            id_target=self._id_target,
            pitch=self._pitch,
            origin=self._origin,
            connectivity=self._connectivity,
        )
        loss.backward()
        self._optimizer.update()
        model.cleargrads()

        loss = float(loss.array)

        print(f'[{self._iteration:08d}] {loss}')
        print(f'quaternion:', model.quaternion.array.tolist())
        print(f'translation:', model.translation.array.tolist())

    @property
    def transform(self):
        model = self._optimizer.target
        quaternion = model.quaternion.array
        translation = model.translation.array
        transform = tf.quaternion_matrix(quaternion)
        transform = objslampp.geometry.compose_transform(
            transform[:3, :3], translation
        )
        return transform

    def nstep(self, iteration):
        yield self.transform
        for _ in range(iteration):
            self.step()
            yield self.transform

    def visualize(self, cad, T_cad2cam_true, T_cad2cam_pred, T_com2cam):
        scenes = {}

        grid_target = self._grid_target
        id_target = self._id_target
        pitch = self._pitch
        origin = self._origin

        origin = tf.transform_points([origin], T_com2cam)[0]

        scene = trimesh.Scene()
        # occupied target/untarget
        voxel = trimesh.voxel.Voxel(
            matrix=grid_target == id_target, pitch=pitch, origin=origin
        )
        geom = voxel.as_boxes((1., 0, 0, 0.5))
        scene.add_geometry(geom, geom_name='occupied_target')
        voxel = trimesh.voxel.Voxel(
            matrix=~np.isin(grid_target, [id_target, 254, 255]),
            pitch=pitch,
            origin=origin,
        )
        geom = voxel.as_boxes((0, 1., 0, 0.5))
        scene.add_geometry(geom, geom_name='occupied_untarget')
        scenes['occupied'] = scene

        # empty
        scene = trimesh.Scene()
        voxel = trimesh.voxel.Voxel(
            matrix=grid_target == 254, pitch=pitch, origin=origin
        )
        geom = voxel.as_boxes((0.5, 0.5, 0.5, 0.5))
        scene.add_geometry(geom, geom_name='empty')
        scenes['empty'] = scene

        scene = trimesh.Scene()
        # cad_true
        cad_copy = cad.copy()
        cad_copy.visual.vertex_colors[:, 3] = 127
        cad_copy.apply_transform(T_cad2cam_true)
        scene.add_geometry(cad_copy, geom_name='cad_true')
        scenes['cad'] = scene

        # cad_pred
        cad_copy = cad.copy()
        cad_copy.apply_transform(T_cad2cam_pred)
        scenes['occupied'].add_geometry(cad_copy, geom_name='cad_pred')
        scenes['empty'].add_geometry(cad_copy, geom_name='cad_pred')
        scenes['cad'].add_geometry(cad_copy, geom_name='cad_pred')

        # bbox
        aabb_min = origin - pitch / 2
        aabb_max = aabb_min + pitch * np.array(grid_target.shape)
        geom = trimesh.path.creation.box_outline(aabb_max - aabb_min)
        geom.apply_translation((aabb_min + aabb_max) / 2)
        scenes['occupied'].add_geometry(geom, geom_name='bbox')
        scenes['empty'].add_geometry(geom, geom_name='bbox')
        scenes['cad'].add_geometry(geom, geom_name='bbox')

        center = scenes['cad'].centroid
        for scene in scenes.values():
            scene.set_camera(
                angles=[np.deg2rad(180), 0, 0],
                distance=0.6,
                center=center,
            )
        return scenes


class SceneOccupancyGridRegistration:

    _models = objslampp.datasets.YCBVideoModels()

    connectivity = 2  # threshold for occupancy grid voxelization

    def __init__(
        self,
        instance_ids,
        class_ids,
        Ts_cad2cam_true,
        pcd,
        instance_label,
    ):
        N = len(instance_ids)
        assert instance_ids.shape == (N,) and 0 not in instance_ids
        assert class_ids.shape == (N,) and 0 not in class_ids
        assert Ts_cad2cam_true.shape == (N, 4, 4)
        H, W = pcd.shape[:2]
        assert pcd.shape == (H, W, 3)
        assert instance_label.shape == (H, W)

        self._class_ids = class_ids
        self._instance_ids = instance_ids
        self._Ts_cad2cam_true = Ts_cad2cam_true
        self._pcd = pcd
        self._instance_label = instance_label
        self._octrees = self.build_octrees(0.01)

    def build_octrees(self, pitch):
        pcd = self._pcd
        instance_ids = self._instance_ids
        instance_label = self._instance_label

        nonnan = ~np.isnan(pcd).any(axis=2)
        octrees = {}
        for ins_id in np.r_[0, instance_ids]:
            mask = instance_label == ins_id
            octree = octomap.OcTree(pitch)
            octree.insertPointCloud(
                pcd[mask & nonnan],
                np.array([0, 0, 0], dtype=float),
            )
            octrees[ins_id] = octree

        return octrees

    def __call__(self, instance_id):
        models = self._models

        # parameters
        connectivity = 2
        dim = 16  # 20
        step = 100

        # scene-level data
        class_ids = self._class_ids
        instance_ids = self._instance_ids
        pcd = self._pcd
        instance_label = self._instance_label
        Ts_cad2cam_true = self._Ts_cad2cam_true
        octrees = self._octrees

        # instance-level data
        index = np.where(instance_ids == instance_id)[0][0]
        instance_id = instance_ids[index]
        class_id = class_ids[index]
        T_cad2cam_true = Ts_cad2cam_true[index]
        cad_file = models.get_cad_model(class_id=class_id)
        diagonal = models.get_bbox_diagonal(cad_file)
        pitch = diagonal / dim
        mask = instance_label == instance_id
        extents = np.array((diagonal,) * 3)
        grid_target, aabb_min, _ = get_instance_grid(
            octrees,
            pitch,
            pcd,
            mask,
            instance_id,
            extents,
            threshold=connectivity * 0.75
        )
        dimension = np.array(grid_target.shape)
        #
        pcd_file = models.get_pcd_model(class_id=class_id)
        points_source = np.loadtxt(pcd_file, dtype=np.float32)
        points_source = contrib.extra.open3d.voxel_down_sample(
            points_source, voxel_size=pitch
        )
        points_source = points_source.astype(np.float32)
        #
        centroid = aabb_min + pitch * dimension / 2
        T_com2cam = tf.translation_matrix(centroid)
        origin = - pitch * dimension / 2

        registration = InstanceOccupancyGridRegistration(
            points_source,
            grid_target,
            id_target=instance_id,
            pitch=pitch,
            origin=origin,
            connectivity=connectivity,
        )
        Ts_cad2com_pred = registration.nstep(step)
        Ts_cad2cam_pred = (T_com2cam @ T for T in Ts_cad2com_pred)

        return registration, Ts_cad2cam_pred, T_cad2cam_true, T_com2cam


def main():
    models = objslampp.datasets.YCBVideoModels()
    dataset = objslampp.datasets.YCBVideoDataset('train')
    frame = dataset.get_example(1000)

    # scene-level data
    instance_ids = class_ids = frame['meta']['cls_indexes']
    Ts_cad2cam_true = np.tile(np.eye(4), (len(instance_ids), 1, 1))
    Ts_cad2cam_true[:, :3, :4] = frame['meta']['poses'].transpose(2, 0, 1)
    K = frame['meta']['intrinsic_matrix']
    pcd = objslampp.geometry.pointcloud_from_depth(
        frame['depth'], fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
    )
    instance_label = frame['label']

    # instance-level data
    instance_id = instance_ids[0]
    index = np.where(instance_ids == instance_id)[0][0]
    class_id = class_ids[index]
    cad_file = models.get_cad_model(class_id=class_id)
    cad = trimesh.load(str(cad_file))
    cad.visual = cad.visual.to_color()

    registration, Ts_cad2cam_pred, T_cad2cam_true, T_com2cam = \
        SceneOccupancyGridRegistration(
            instance_ids=instance_ids,
            class_ids=class_ids,
            Ts_cad2cam_true=Ts_cad2cam_true,
            pcd=pcd,
            instance_label=instance_label,
        )(instance_id)

    # -------------------------------------------------------------------------

    window = pyglet.window.Window(width=640 * 3, height=480)
    window.play = False

    @window.event
    def on_key_press(symbol, modifiers):
        if modifiers == 0:
            if symbol == pyglet.window.key.Q:
                window.on_close()
            elif symbol == pyglet.window.key.S:
                window.play = not window.play

    def callback(dt, widgets=None):
        if widgets and not window.play:
            return
        try:
            T_cad2cam_pred = next(Ts_cad2cam_pred)
        except StopIteration:
            pyglet.clock.unschedule(callback)
            return
        scenes = registration.visualize(
            cad=cad,
            T_cad2cam_true=T_cad2cam_true,
            T_cad2cam_pred=T_cad2cam_pred,
            T_com2cam=T_com2cam,
        )
        if widgets:
            for key, widget in widgets.items():
                widget.scene.geometry.update(scenes[key].geometry)
                widget.scene.graph.load(scenes[key].graph.to_edgelist())
                widget._draw()
        return scenes

    gui = glooey.Gui(window)

    hbox = glooey.HBox()
    hbox.set_padding(5)
    widgets = {}
    scenes = callback(-1)
    for key, scene in scenes.items():
        widgets[key] = trimesh.viewer.SceneWidget(scene)
        vbox = glooey.VBox()
        vbox.add(glooey.Label(key, color=(255, 255, 255)), size=0)
        vbox.add(widgets[key])
        hbox.add(vbox)
    gui.add(hbox)

    pyglet.clock.schedule_interval(callback, 1 / 30, widgets)
    pyglet.app.run()


if __name__ == '__main__':
    main()
