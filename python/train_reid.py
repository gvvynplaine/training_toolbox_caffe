import caffe
import cv2
import numpy as np
import seaborn as sns
import glob
from argparse import ArgumentParser
from multiprocessing import Process
from os.path import exists, join, basename
from collections import namedtuple
from scipy.ndimage.filters import gaussian_filter
from tqdm import tqdm, trange
from os import makedirs
from time import strftime, localtime


SampleDesc = namedtuple('SampleDesc', 'path label')
DataFormat = namedtuple('DataFormat', 'id_start_pos id_end_pos camera_start_pos camera_end_pos')
PointDesc = namedtuple('PointDesc', 'values, iteration')


class SampleDataFromDisk:
    def __init__(self, data_file_path):
        self._all_samples = {}
        self._ids_by_label = {}

        with open(data_file_path) as f:
            all_lines = f.readlines()
            num_lines = len(all_lines)
            for i in range(num_lines):
                line = all_lines[i]
                arr = line.strip().split()

                image_path = arr[0]
                label = int(arr[1])

                self._all_samples[i] = SampleDesc(path=image_path, label=label)
                self._ids_by_label[label] = self._ids_by_label.get(label, []) + [i]

        print('Number of classes: {}'.format(len(self._ids_by_label)))
        print('Number of training images: {}'.format(len(self._all_samples)))

    def get_image(self, sample_id):
        sample_desc = self._all_samples[sample_id]
        image = cv2.imread(sample_desc.path)

        return image, sample_desc.label

    def get_all_ids(self):
        return self._all_samples.keys()

    def get_ids_by_labels(self, label):
        return self._ids_by_label.get(label, [])

    def get_all_labels(self):
        return self._ids_by_label.keys()

    def get_num_labels(self):
        return len(self._ids_by_label.keys())


class SolverWrapper:
    def _init_log(self):
        glog_dir = join(self.param.working_dir, 'logs', 'caffe')
        caffe.init_log_level_pipe_place(2, False, glog_dir)

        log_file_name = 'log-{}.txt'.format(strftime('%b-%d-%Y_%H-%M-%S', localtime()))
        self.log_stream = open(join(self.param.working_dir, 'logs', log_file_name), 'w')

    def _release_log(self):
        self.log_stream.close()

    @staticmethod
    def _set_device(use_gpu, device_id):
        if use_gpu:
            caffe.set_mode_gpu()
            caffe.set_device(device_id)
        else:
            caffe.set_mode_cpu()

    @staticmethod
    def _image_to_blob(img, img_height, img_width):
        img = cv2.resize(img, (img_width, img_height), interpolation=cv2.INTER_LINEAR)

        blob = img.astype(np.float32)
        blob = blob.transpose((2, 0, 1))

        return blob

    def _reset_states(self):
        self.train_iter = 0

        self.image_blobs = np.empty([self.num_samples, 3,
                                     self.image_size_[0], self.image_size_[1]], dtype=np.float32)
        self.label_blobs = np.empty([self.num_samples], dtype=np.float32)

    def _init_solver(self, solver_proto, weights=None, snapshot=None):
        self.solver = caffe.SGDSolver(solver_proto)
        if snapshot is not None and exists(snapshot):
            self.solver.restore(snapshot)
            self._log('Loaded solver snapshot: {}'.format(snapshot), True)
        elif weights is not None:
            weights_paths = weights.split(',')
            for weight_path in weights_paths:
                if exists(weight_path):
                    for test_net_id in xrange(len(self.solver.test_nets)):
                        self.solver.test_nets[test_net_id].copy_from(weight_path)
                    self.solver.net.copy_from(weight_path)
                    self._log('Loaded pre-trained model weights from: {}'.format(weight_path), True)

        layer_names = list(self.solver.net._layer_names)
        layer_idx = {name: i for i, name in enumerate(layer_names)}
        self.train_outputs = {name: layer_idx[name] for name in self.solver.net.outputs}

    def _init(self):
        self._init_log()
        self._set_device(not self.param.use_cpu, self.param.gpu_id)

        self._init_solver(self.param.solver, self.param.weights, self.param.snapshot)

        self.virtual_batch_size = self.param.batch_size * self.solver.param.iter_size

        num_hardest = int(self.num_samples * self.param.sampler_fraction)
        num_hardest = ((num_hardest / self.virtual_batch_size) + 1) * self.virtual_batch_size
        self.num_hardest = np.minimum(self.num_samples, num_hardest)

        self._load_test_data()

    @staticmethod
    def _parse_data_format(format_name):
        if format_name == 'MARS':
            data_format = DataFormat(id_start_pos=0, id_end_pos=4,
                                     camera_start_pos=5, camera_end_pos=6)
        elif format_name == 'Market':
            data_format = DataFormat(id_start_pos=0, id_end_pos=4,
                                     camera_start_pos=6, camera_end_pos=7)
        elif format_name == 'Duke':
            data_format = DataFormat(id_start_pos=0, id_end_pos=4,
                                     camera_start_pos=6, camera_end_pos=7)
        elif format_name == 'Datatang':
            data_format = DataFormat(id_start_pos=0, id_end_pos=12,
                                     camera_start_pos=-1, camera_end_pos=-1)
        elif format_name == 'Viper':
            data_format = DataFormat(id_start_pos=0, id_end_pos=3,
                                     camera_start_pos=-1, camera_end_pos=-1)
        else:
            raise Exception('Unknown data format: {}'.format(format_name))

        return data_format

    def _parse_data(self, data_dir, data_format, task_name):
        def _parse_int(full_str, start, end):
            if 0 <= start < end:
                return int(full_str[start:end])
            else:
                return 0

        image_blobs = []
        label_blobs = []
        for file_name in tqdm(glob.glob('{}/*'.format(data_dir)), desc='Reading {} data'.format(task_name)):
            path = file_name
            name = basename(file_name)

            if name.startswith('-'):
                continue

            image = cv2.imread(path)
            person_id = _parse_int(name, data_format.id_start_pos, data_format.id_end_pos)

            image_blobs.append(self._image_to_blob(image, self.image_size_[0], self.image_size_[1]))
            label_blobs.append(float(person_id))

        image_blobs = np.array(image_blobs, dtype=np.float32)
        label_blobs = np.array(label_blobs, dtype=np.float32)

        return image_blobs, label_blobs

    def _load_test_data(self):
        if exists(self.param.query_dir) and exists(self.param.gallery_dir):
            data_format = self._parse_data_format(self.param.format)

            self.query_image_blobs, self.query_label_blobs =\
                self._parse_data(self.param.query_dir, data_format, 'query')
            self.query_zero_label_blobs = np.zeros_like(self.query_label_blobs)

            self.gallery_image_blobs, self.gallery_label_blobs =\
                self._parse_data(self.param.gallery_dir, data_format, 'gallery')
            self.gallery_zero_label_blobs = np.zeros_like(self.gallery_label_blobs)
        else:
            self.query_image_blobs = None
            self.query_label_blobs = None
            self.query_zero_label_blobs = None
            self.gallery_image_blobs = None
            self.gallery_label_blobs = None
            self.gallery_zero_label_blobs = None

    def __init__(self, param):
        self.param = param

        data_sampler = SampleDataFromDisk(self.param.data_file)
        self.data_sampler_ = data_sampler
        self.num_images_ = self.param.num_images
        self.image_size_ = self.param.image_size

        self.num_samples = self.data_sampler_.get_num_labels() * self.num_images_

        self._reset_states()

    def _log(self, message, save_log=False, new_line_before=False):
        full_message = '{}: {}'.format(strftime('%b-%d-%Y_%H-%M-%S', localtime()), message)
        if new_line_before:
            full_message = '\n' + full_message

        print(full_message)

        if save_log:
            self.log_stream.write('{}\n'.format(full_message))
            self.log_stream.flush()

    def _augment(self, img, trg_height, trg_width):
        augmented_img = img

        if self.param.dither:
            middle_aspect = 0.5 * (self.param.aspect_ratio_limits[0] + self.param.aspect_ratio_limits[1])
            min_aspect = middle_aspect - (middle_aspect - self.param.aspect_ratio_limits[0]) * self.difficulty
            max_aspect = middle_aspect + (self.param.aspect_ratio_limits[1] - middle_aspect) * self.difficulty
            crop_aspect = np.random.uniform(min_aspect, max_aspect)
            crop_height = trg_height
            crop_width = int(float(crop_height) / crop_aspect)

            border_size = np.random.uniform(0.0, float(self.param.max_border_size) * self.difficulty)
            region_height = int(crop_height + 2.0 * border_size)
            region_width = int(crop_width + 2.0 * border_size)

            src_aspect_ratio = float(augmented_img.shape[0]) / float(augmented_img.shape[1])
            trg_aspect_ratio = float(region_height) / float(region_width)

            if src_aspect_ratio > trg_aspect_ratio:
                width = region_width
                height = int(float(width) * src_aspect_ratio)
            else:
                height = region_height
                width = int(float(height) / src_aspect_ratio)
            augmented_img = cv2.resize(augmented_img, (width, height))

            crop_height = np.minimum(crop_height, augmented_img.shape[0])
            crop_width = np.minimum(crop_width, augmented_img.shape[1])

            height_diff = augmented_img.shape[0] - crop_height
            width_diff = augmented_img.shape[1] - crop_width

            left_edge = np.random.randint(0, width_diff + 1)
            right_edge = left_edge + crop_width
            top_edge = np.random.randint(0, height_diff + 1)
            bottom_edge = top_edge + crop_height

            augmented_img = augmented_img[top_edge:bottom_edge, left_edge:right_edge]
            augmented_img = cv2.resize(augmented_img, (trg_width, trg_height))

        if self.param.blur:
            if np.random.uniform(0.0, 1.0) < np.minimum(self.param.max_blur_prob, self.difficulty):
                filter_size = np.random.uniform(low=self.param.sigma_limits[0], high=self.param.sigma_limits[1])
                augmented_img[:, :, 0] = gaussian_filter(augmented_img[:, :, 0], sigma=filter_size)
                augmented_img[:, :, 1] = gaussian_filter(augmented_img[:, :, 1], sigma=filter_size)
                augmented_img[:, :, 2] = gaussian_filter(augmented_img[:, :, 2], sigma=filter_size)

        if self.param.mirror:
            if np.random.randint(0, 2) == 1:
                augmented_img = augmented_img[:, ::-1, :]

        if self.param.gamma:
            if np.random.uniform(0.0, 1.0) < np.minimum(self.param.max_gamma_prob, self.difficulty):
                u = np.random.uniform(-self.param.delta, self.param.delta)
                gamma = np.log(0.5 + (2 ** (-0.5)) * u) / np.log(0.5 - (2 ** (-0.5)) * u)

                float_image = augmented_img.astype(np.float32) * (1. / 255.)
                augmented_img = (np.power(float_image, gamma) * 255.0).astype(np.int32)
                augmented_img[augmented_img > 255] = 255
                augmented_img[augmented_img < 0] = 0
                augmented_img = augmented_img.astype(np.uint8)

        if self.param.brightness:
            if np.random.uniform(0.0, 1.0) < np.minimum(self.param.max_brightness_prob, self.difficulty):
                if np.average(augmented_img) > self.param.min_pos:
                    alpha = np.random.uniform(self.param.pos_alpha[0], self.param.pos_alpha[1])
                    beta = np.random.randint(self.param.pos_beta[0], self.param.pos_beta[1])
                else:
                    alpha = np.random.uniform(self.param.neg_alpha[0], self.param.neg_alpha[1])
                    beta = np.random.randint(self.param.neg_beta[0], self.param.neg_beta[1])

                augmented_img = (augmented_img.astype(np.float32) * alpha + beta).astype(np.int32)
                augmented_img[augmented_img > 255] = 255
                augmented_img[augmented_img < 0] = 0
                augmented_img = augmented_img.astype(np.uint8)

        if self.param.erase:
            if np.random.uniform(0.0, 1.0) < np.minimum(self.param.max_erase_prob, self.difficulty):
                width = augmented_img.shape[1]
                height = augmented_img.shape[0]

                num_erase_iter = np.random.randint(self.param.erase_num[0], self.param.erase_num[1])
                for _ in xrange(num_erase_iter):
                    erase_width = int(np.random.uniform(self.param.erase_size[0], self.param.erase_size[1]) * width)
                    erase_height = int(np.random.uniform(self.param.erase_size[0], self.param.erase_size[1]) * height)

                    left_edge = int(np.random.uniform(self.param.erase_border[0], self.param.erase_border[1]) * width)
                    top_edge = int(np.random.uniform(self.param.erase_border[0], self.param.erase_border[1]) * height)
                    right_edge = np.minimum(left_edge + erase_width, width)
                    bottom_edge = np.minimum(top_edge + erase_height, height)

                    if np.random.randint(0, 2) == 1:
                        fill_color = np.random.randint(0, 255, size=[bottom_edge - top_edge,
                                                                     right_edge - left_edge, 3], dtype=np.uint8)
                    else:
                        fill_color = np.random.randint(0, 255, size=3, dtype=np.uint8)
                    augmented_img[top_edge:bottom_edge, left_edge:right_edge] = fill_color

        return augmented_img.astype(np.uint8)

    def _sample_data_ids(self):
        all_labels = np.copy(self.data_sampler_.get_all_labels())
        np.random.shuffle(all_labels)

        data_ids = []
        while len(data_ids) < self.num_samples:
            for label in all_labels:
                label_ids = self.data_sampler_.get_ids_by_labels(label)
                if len(label_ids) <= 0:
                    continue

                data_ids.append(np.random.choice(label_ids, 1)[0])

        data_ids = np.array(data_ids[:self.num_samples]).reshape([-1])

        return data_ids

    def _prepare_data(self, data_ids):
        for i in trange(len(data_ids), desc='Collecting blobs'):
            data_id = data_ids[i]
            image, label = self.data_sampler_.get_image(data_id)

            augmented_image = self._augment(image, self.image_size_[0], self.image_size_[1])

            image_blob = self._image_to_blob(augmented_image, self.image_size_[0], self.image_size_[1])
            label_blob = float(label)

            self.image_blobs[i] = image_blob
            self.label_blobs[i] = label_blob

    @staticmethod
    def _calc_metrics(distances, gallery_labels, query_labels):
        def _compute_ap(good_inds, top_inds):
            loc_cmc = np.zeros([len(top_inds)], dtype=np.float32)
            num_good_total = len(good_inds)

            old_recall = 0.
            old_precision = 1.
            loc_ap = 0.
            intersect_size = 0
            j = 0
            num_good_now = 0

            for n, top_index in enumerate(top_inds):
                flag = False
                if top_index in good_inds:
                    loc_cmc[n:] = 1
                    flag = True
                    num_good_now += 1

                if flag:
                    intersect_size += 1

                recall = float(intersect_size) / float(num_good_total) if num_good_total > 0 else 0.0
                precision = float(intersect_size) / float(j + 1)
                loc_ap += 0.5 * (recall - old_recall) * (old_precision + precision)
                old_recall = recall
                old_precision = precision
                j += 1

                if num_good_now == num_good_total:
                    break

            return loc_ap, loc_cmc

        rank_size = distances.shape[0]

        ap_all = np.zeros([len(query_labels)], dtype=np.float32)
        cmc_all = np.zeros([len(query_labels), rank_size], dtype=np.float32)

        for k in trange(len(query_labels), desc='Calculating metric'):
            query_label = int(query_labels[k])
            good_indices = [gallery_id for gallery_id, gallery_label in enumerate(gallery_labels)
                            if int(gallery_label) == query_label]

            scores = distances[:, k]
            indexed_scores = [(s_index, s) for s_index, s in enumerate(scores)]
            indexed_scores.sort(key=lambda x: x[1], reverse=False)
            top_indices = [t[0] for t in indexed_scores[:rank_size]]

            ap_k, cmc_k = _compute_ap(good_indices, top_indices)
            ap_all[k] = ap_k
            cmc_all[k, :] = cmc_k

        ap = np.mean(ap_all)
        cmc = np.mean(cmc_all, axis=0)

        return ap, cmc

    def _test_model(self):
        if self.query_image_blobs is not None and\
           self.query_label_blobs is not None and \
           self.query_zero_label_blobs is not None and\
           self.gallery_image_blobs is not None and\
           self.gallery_label_blobs is not None and \
           self.gallery_zero_label_blobs is not None:
            self._log('Inference query...')
            net_output = self.solver.test_nets[0].forward_all(
                data=self.query_image_blobs, label=self.query_zero_label_blobs)
            query_embeddings = net_output['embd_out']

            self._log('Inference gallery...')
            net_output = self.solver.test_nets[0].forward_all(
                data=self.gallery_image_blobs, label=self.gallery_zero_label_blobs)
            gallery_embeddings = net_output['embd_out']

            distances = 1. - np.matmul(gallery_embeddings, np.transpose(query_embeddings))

            ap, cmc = self._calc_metrics(distances, self.gallery_label_blobs, self.query_label_blobs)
            self._log('Rank@1: {} Rank@5: {} mAP: {}'.format(cmc[0], cmc[4], ap), True)

            return cmc[0]

    def _estimate_losses(self):
        self._log('Estimating losses...')
        net_output = self.solver.test_nets[0].forward_all(
            data=self.image_blobs, label=self.label_blobs)
        losses = net_output[self.param.output_name]
        return losses

    def _find_hardest_samples(self, losses):
        indexed_losses = [(i, l) for i, l in enumerate(losses)]
        indexed_losses.sort(key=lambda x: x[1], reverse=True)
        indexed_losses = indexed_losses[:self.num_hardest]

        sample_ids = np.array([t[0] for t in indexed_losses], dtype=np.int32)

        self._log('Min loss: {} Mean loss: {} Max loss: {} Std: {}'
                  .format(np.min(losses), np.mean(losses), np.max(losses), np.std(losses)), True)

        if np.std(losses) > 0.0:
            sns_dist_ax = sns.distplot(losses, norm_hist=True)
            sns_dist_ax.axvline(x=indexed_losses[-1][1], color='r')
            sns_dist_fig = sns_dist_ax.get_figure()

            image_name = 'dist_{:06}.png'.format(self.train_iter)
            sns_dist_fig.savefig(join(self.param.working_dir, 'logs', 'dist', image_name))
            sns_dist_fig.clf()

        return sample_ids

    def _train(self, ids):
        assert len(ids) >= self.virtual_batch_size
        assert len(ids) % self.virtual_batch_size == 0

        self.solver.net.layers[0].update_data(self.image_blobs, self.label_blobs)
        self.solver.net.layers[0].update_indices(ids, shuffle=True)

        num_train_iter = len(ids) / self.virtual_batch_size
        for local_train_iter in xrange(num_train_iter):
            self.solver.step(1)

            self._log('Train iter: {} / {}'.format(local_train_iter + 1, num_train_iter), True, True)
            for output_name in self.solver.net.outputs:
                output_data = self.solver.net.blobs[output_name].data
                if output_data.size == 1:
                    self._log('Output {}: {}'.format(output_name, output_data.reshape([-1])[0]), True)

    def _save_state(self):
        self.solver.snapshot()

    def _solve(self):
        best_rank1_acc = 0.0
        best_iter = 0

        while self.train_iter < self.solver.param.max_iter:
            self._log('MetaIter #{}'.format(self.train_iter), True, True)

            difficulty = float(self.train_iter) * float(self.param.max_difficulty) / float(
                self.param.max_difficulty_iter)
            self.difficulty = np.minimum(self.param.max_difficulty, difficulty)
            self._log('Difficulty: {}'.format(self.difficulty), True)

            rank1_acc = self._test_model()
            if rank1_acc > best_rank1_acc:
                best_rank1_acc = rank1_acc
                best_iter = self.train_iter - 1

            data_ids = self._sample_data_ids()
            self._prepare_data(data_ids)

            loss_values = self._estimate_losses()
            hard_sample_ids = self._find_hardest_samples(loss_values)

            self._train(hard_sample_ids)

            self._save_state()
            self._log('Current best model #{}: {} rank@1 accuracy'.format(best_iter, best_rank1_acc), True, True)

            self.train_iter += 1

        self._release_log()

    def _process(self):
        self._init()
        self._solve()

    def train(self):
        p = Process(target=self._process)
        p.daemon = True

        p.start()
        p.join()


def prepare_directory(working_dir_path):
    if not exists(working_dir_path):
        makedirs(working_dir_path)
        makedirs(join(working_dir_path, 'logs', 'dist'))
        makedirs(join(working_dir_path, 'logs', 'caffe'))
        makedirs(join(working_dir_path, 'snapshots'))
    else:
        if not exists(join(working_dir_path, 'logs', 'dist')):
            makedirs(join(working_dir_path, 'logs', 'dist'))

        if not exists(join(working_dir_path, 'logs', 'caffe')):
            makedirs(join(working_dir_path, 'logs', 'caffe'))

        if not exists(join(working_dir_path, 'snapshots')):
            makedirs(join(working_dir_path, 'snapshots'))

if __name__ == '__main__':
    def _list_to_ints(s):
        return [int(v) for v in s.split(',')]

    def _list_to_floats(s):
        return [float(v) for v in s.split(',')]

    parser = ArgumentParser()
    parser.add_argument('--data_file', '-d', required=True, help='Path to .txt file with annotated images.')
    parser.add_argument('--solver', '-s', required=True, help='Solver proto definition.')
    parser.add_argument('--working_dir', '-w', required=True, help='Working directory')
    parser.add_argument('--weights', default=None, help='Model weights to restore.')
    parser.add_argument('--snapshot', default=None, help='Solver snapshot to restore.')
    parser.add_argument('--use_cpu', action='store_true', help='Use CPU device.')
    parser.add_argument('--gpu_id', type=int, default=0, help='GPU device id')
    parser.add_argument('--batch_size', type=int, default=128, help='Mini-batch size')
    parser.add_argument('--num_images', type=int, default=4, help='Number of images per ID')
    parser.add_argument('--image_size', type=_list_to_ints, default='120,48', help='Image size: height,width')
    parser.add_argument('--max_difficulty', type=float, default=1.0, help='')
    parser.add_argument('--max_difficulty_iter', type=int, default=500, help='')
    parser.add_argument('--dither', type=bool, default=True, help='')
    parser.add_argument('--aspect_ratio_limits', type=_list_to_floats, default='1.8,3.2', help='')
    parser.add_argument('--max_border_size', type=int, default=6, help='')
    parser.add_argument('--blur', type=bool, default=True, help='')
    parser.add_argument('--max_blur_prob', type=float, default=0.5, help='')
    parser.add_argument('--sigma_limits', type=_list_to_floats, default='0.0,0.5', help='')
    parser.add_argument('--mirror', type=bool, default=True, help='')
    parser.add_argument('--gamma', type=bool, default=True, help='')
    parser.add_argument('--max_gamma_prob', type=float, default=0.5, help='')
    parser.add_argument('--delta', type=float, default=0.01, help='')
    parser.add_argument('--brightness', type=bool, default=True, help='')
    parser.add_argument('--max_brightness_prob', type=float, default=0.5, help='')
    parser.add_argument('--min_pos', type=float, default=128.0, help='')
    parser.add_argument('--pos_alpha', type=_list_to_floats, default='0.2,1.1', help='')
    parser.add_argument('--pos_beta', type=_list_to_floats, default='-20.0,10.0', help='')
    parser.add_argument('--neg_alpha', type=_list_to_floats, default='0.9,1.5', help='')
    parser.add_argument('--neg_beta', type=_list_to_floats, default='-10.0,20.0', help='')
    parser.add_argument('--erase', type=bool, default=True, help='')
    parser.add_argument('--max_erase_prob', type=float, default=0.5, help='')
    parser.add_argument('--erase_num', type=_list_to_ints, default='1,4', help='')
    parser.add_argument('--erase_size', type=_list_to_floats, default='0.3,0.6', help='')
    parser.add_argument('--erase_border', type=_list_to_floats, default='0.1,0.9', help='')
    parser.add_argument('--input_name', default='data', help='')
    parser.add_argument('--output_name', default='softmax_loss', help='')
    parser.add_argument('--sampler_fraction', type=float, default=0.4, help='')
    parser.add_argument('--query_dir', dest='query_dir', type=str, required=False, default='',
                        help='Path to the dir with query images')
    parser.add_argument('--gallery_dir', dest='gallery_dir', type=str, required=False, default='',
                        help='Path to the dir with gallery images')
    parser.add_argument('--format', dest='format', type=str,
                        choices=['MARS', 'Market', 'Duke', 'Datatang', 'Viper'], default='Datatang',
                        help='Name format according to dataset name')
    args = parser.parse_args()

    assert exists(args.data_file)
    assert exists(args.solver)

    prepare_directory(args.working_dir)

    solver = SolverWrapper(args)
    solver.train()