#! /usr/bin/python

import os
import sys
import json
import pickle

import luigi
import numpy as np
import nifty.tools as nt

import cluster_tools.utils.volume_utils as vu
import cluster_tools.utils.function_utils as fu
from cluster_tools.cluster_tasks import SlurmTask, LocalTask, LSFTask


#
# Block Face Tasks
#

class BlockFacesBase(luigi.Task):
    """ BlockFaces base class
    """

    task_name = 'block_faces'
    src_file = os.path.abspath(__file__)
    allow_retry = False

    input_path = luigi.Parameter()
    input_key = luigi.Parameter()
    offsets_path = luigi.Parameter()
    # task that is required before running this task
    dependency = luigi.TaskParameter()

    def requires(self):
        return self.dependency

    def run_impl(self):
        shebang, block_shape, roi_begin, roi_end = self.global_config_values()
        self.init(shebang)

        # get shape and make block config
        shape = vu.get_shape(self.input_path, self.input_key)
        config = self.get_task_config()
        config.update({'input_path': self.input_path,
                       'input_key': self.input_key,
                       'offsets_path': self.offsets_path,
                       'block_shape': block_shape,
                       'tmp_folder': self.tmp_folder})

        block_list = vu.blocks_in_volume(shape, block_shape,
                                         roi_begin, roi_end)

        n_jobs = min(len(block_list), self.max_jobs)

        # we only have a single job to find the labeling
        self.prepare_jobs(n_jobs, block_list, config)
        self.submit_jobs(n_jobs)

        # wait till jobs finish and check for job success
        self.wait_for_jobs()
        # log the save-path again
        self.check_jobs(n_jobs)


class BlockFacesLocal(BlockFacesBase, LocalTask):
    """
    BlockFaces on local machine
    """
    pass


class BlockFacesSlurm(BlockFacesBase, SlurmTask):
    """
    BlockFaces on slurm cluster
    """
    pass


class BlockFacesLSF(BlockFacesBase, LSFTask):
    """
    BlockFaces on lsf cluster
    """
    pass


def _process_face(ds, offsets, face, face_a, face_b,
                  block_a, block_b):
    off_a = offsets[block_a]
    off_b = offsets[block_b]
    seg = ds[face]

    # load the local faces
    labels_a = seg[face_a].squeeze()
    labels_b = seg[face_b].squeeze()
    assert labels_a.size > 0
    assert labels_a.shape == labels_b.shape

    have_labels = np.logical_and(labels_a != 0, labels_b != 0)
    labels_a = labels_a[have_labels]
    labels_b = labels_b[have_labels]
    assert labels_a.shape == labels_b.shape

    if labels_a.size == 0:
        return None

    # add the offsets that make the block ids unique
    labels_a += off_a
    labels_b += off_b

    assignments = np.concatenate((labels_a[:, None], labels_b[:, None]), axis=1)
    assignments = np.unique(assignments, axis=0)
    return assignments


def _process_faces(block_id, blocking, ds, offsets, empty_blocks):
    fu.log("start processing block %i" % block_id)
    if block_id in empty_blocks:
        fu.log_block_success(block_id)
        return None

    assignments = [_process_face(ds, offsets, face,
                                 face_a, face_b, block_a, block_b)
                   for face, face_a, face_b, block_a, block_b
                   in vu.iterate_faces(blocking, block_id,
                                       return_only_lower=True,
                                       empty_blocks=empty_blocks)]
    assignments = [ass for ass in assignments if ass is not None]

    # all assignments might be None, so we need to check for that
    if assignments:
        assignments = np.unique(np.concatenate(assignments, axis=0),
                                axis=0)
    else:
        assignments = None
    fu.log_block_success(block_id)
    return assignments


def block_faces(job_id, config_path):

    fu.log("start processing job %i" % job_id)
    fu.log("reading config from %s" % config_path)

    with open(config_path, 'r') as f:
        config = json.load(f)
    input_path = config['input_path']
    input_key = config['input_key']
    block_list = config['block_list']
    tmp_folder = config['tmp_folder']
    offsets_path = config['offsets_path']
    block_shape = config['block_shape']

    with open(offsets_path) as f:
        offsets_dict = json.load(f)
        offsets = offsets_dict['offsets']
        empty_blocks = offsets_dict['empty_blocks']
        n_labels = offsets_dict['n_labels']

    with vu.file_reader(input_path, 'r') as f:
        ds = f[input_key]
        shape = list(ds.shape)

        blocking = nt.blocking([0, 0, 0], shape, block_shape)
        assignments = [_process_faces(block_id, blocking, ds,
                                      offsets, empty_blocks)
                       for block_id in block_list]
    # filter out empty assignments
    assignments = [ass for ass in assignments if ass is not None]
    if assignments:
        assignments = np.concatenate(assignments, axis=0)
        assignments = np.unique(assignments, axis=0)
        assert assignments.max() < n_labels, "%i, %i" % (int(assignments.max()), n_labels)

    save_path = os.path.join(tmp_folder, 'cc_assignments_%i.npy' % job_id)
    np.save(save_path, assignments)
    fu.log_job_success(job_id)


if __name__ == '__main__':
    path = sys.argv[1]
    assert os.path.exists(path), path
    job_id = int(os.path.split(path)[1].split('.')[0].split('_')[-1])
    block_faces(job_id, path)
