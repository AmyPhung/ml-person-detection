#!/usr/bin/env python

import sys # Needed for relative imports
sys.path.append('../') # Needed for relative imports

import datetime
import glob
import json
import logging
import os.path
import pdb

import rospy as rp
import numpy as np
import tensorflow as tf

from collections import namedtuple
from modules.helperFunctions import *
from modules.waymo2ros import Waymo2Numpy, Waymo2Ros
from sensor_msgs.msg import PointCloud2
from visualization_msgs.msg import MarkerArray


XYPair = namedtuple('XYPair', 'x y')
XYZPair = namedtuple('XYZPair', 'x y z')


class DatasetCreator(object):
    """Class for creating labeled cluster metadata from raw waymo data.

    Use this class to iterate through .tfrecord files, pull out frames,
    extract clusters from the frames using provided bounding boxes,
    calculate features of clusters, and save resulting features and
    metadata to create a cleaned dataset.

    """

    def __init__(self, dir_load, dir_save, logger=None, dir_log=None, verbosity=None):
        """Provide directory location to find frames."""
        self.waymo_converter = Waymo2Numpy()
        self.dir_load = dir_load
        self.dir_save = dir_save

        # Set up logger if not given as arg
        if logger is not None:
            self.logger = logger
        else:
            # Generate log file name
            d = datetime.datetime.now()
            filename = ('%s/%i-%i-%i-%i-%i.log'
                        % (dir_log, d.year, d.month, d.day, d.hour, d.minute))

            # Create logger with file and stream handlers
            self.logger = logging.getLogger('datasetCreator')
            sh = logging.StreamHandler(sys.stdout)
            fh = logging.FileHandler(filename)

            # Format file and stream logging
            formatter = logging.Formatter(
                '%(asctime)s %(levelname)s %(message)s')
            fh.setFormatter(formatter)
            fh.setLevel(logging.DEBUG)
            sh.setFormatter(formatter)
            sh.setLevel(logging.INFO)
            self.logger.addHandler(fh)
            self.logger.addHandler(sh)
            self.logger.setLevel(logging.DEBUG)

        self.logger.info('Logging set up for createDataset object')
        self.logger.debug('Exit:__init__')

    def filterPcl(self, pcl):
        """Remove groundplane from pcl."""
        self.logger.debug('Entr:filterPcl')

        pcl_out = remove_groundplane(np.array([list(pt) for pt in pcl]))
        self.logger.debug('Show:pts_removed=%i' % (len(pcl) - len(pcl_out)))
        self.logger.debug('Exit:filterPcl')
        return pcl_out

    def clusterByBBox(self, pcl, bboxes, thresh=5):
        """Extract points from pcl within bboxes as clusters.

        Args:
            pcl: (n * 4) numpy array of xyz points and intensities
            bboxes: waymo pcl label output
            thresh: min int point num for cluster (relatively small threshold -
                kept to prevent math errors in metadata computation)

        Returns:
            obj_pcls: Hash map of bbox label : pcl where the pcl contains
                (n * 4) numpy arrays of xyz points and intensities
        """

        self.logger.debug('Entr:clusterByBBox')

        obj_pcls = {}  # Hash map of bbox label : pcl
        self.logger.debug("bbox initial count: %i" % len(bboxes))

        for i, label in enumerate(bboxes):

            cluster = get_pts_in_bbox(pcl, label, self.logger)
            self.logger.debug(
                "bbox=%i * %i, class=%i, id=%s, pt_count=%i"
                % (i, len(bboxes), label.type, label.id, len(cluster)))

            # Threshold cluster size
            if len(cluster) >= thresh:
                obj_pcls[label.id] = cluster
            else:
                obj_pcls[label.id] = None
                self.logger.debug(
                    "cluster_size=%i under threshold=%i"\
                    % (len(cluster), thresh))

            #if i == 5: break  # Uncomment to use subset of bboxes for debug

        self.logger.debug('Exit:clusterByBBox')
        self.logger.debug("bbox final count: %i" % len([o for o in obj_pcls if o is not None]))
        return obj_pcls

    def computeClusterMetadata(self, cluster, bbox, frame_id):
        """Compute key information from cluster to boil down pointcloud info.

        Args:
            cluster: list of xyz points and intensities within cluster
            bbox: waymo object label output
            frame_id: int of frame index into tfrecord

        Returns:
            features: Features object containing cluster features
        """

        self.logger.debug('Entr:computeClusterMetadata')
        if cluster is None:
            raise TypeError(
                'None passed as cluster - ' \
                + 'possibly a too-small cluster passed from clusterByBBox?')
        np_cluster = np.array(cluster)

        features = Features()
        features.cluster_id = bbox.id
        features.frame_id = frame_id
        features.cls = bbox.type
        features.cnt = cluster.shape[0]
        features.parameters = extract_cluster_parameters(np_cluster, display=False)

        self.logger.debug('Exit:computeClusterMetadata')
        return features

    def filterMetadata(self, metadata, clusters, thresh=20):
        """Removes clusters with a density smaller than the specified threshold
        from the dataset

        Args:
            metadata: list of Features objects containing key information about
                each cluster in frame
            clusters: Hash map of bbox label : pcl where the pcl contains
                (n * 4) numpy arrays of xyz points and intensities with None
                types removed
            thresh: minimum number of points per cubic meter

        Returns:
            filtered_metadata: list of Features objects which meet the minimum
                density requirement
            filtered_clusters: list of (n * 4) numpy arrays of xyz points and
                intensities corresponding with Features that meet the min
                density requirement
        """
        filtered_metadata = []
        filtered_clusters = {}

        for i in range(len(metadata)):
            c = metadata[i]

            if c.parameters[7] > thresh: # 7th parameter is density
                filtered_clusters[c.cluster_id] = clusters[c.cluster_id]
                filtered_metadata.append(c)
            # else:
            #     self.logger.warning("Sparse cluster detected in\
            #         filterMetadata")

        return filtered_metadata, filtered_clusters

    def saveClusterMetadata(self, metadata, name):
        """Save cluster metadata from frame in a .json file. Uses frame name as
        .json filename

        Args:
            metadata: list of Features objects containing key information about
                each cluster in frame
            name: name of frame
        """
        self.logger.debug('Entr:saveClusterMetadata')
        filename = '%s/%s.json' % (self.dir_save, str(name))
        self.logger.debug('save_loc=%s' % filename)

        # lambda function is used to serialize custom Features object
        with open(filename, 'w') as outfile:
            json.dump(metadata, outfile, default=lambda o: o.as_dict(), indent=4)

        self.logger.debug('Exit:saveClusterMetadata')

    def parseFrame(self, frame, frame_id):
        """Extract and save data from a single given frame.

        Args:
            frame: waymo open dataset Frame with loaded data
            frame_id: index of waymo Frame in tfrecord

        """
        self.logger.debug('Entr:parseFrame')
        pcl, bboxes = self.waymo_converter.unpack_frame(frame)
        pcl = self.filterPcl(pcl)

        clusters = self.clusterByBBox(pcl, bboxes)
        metadata = [self.computeClusterMetadata(clusters[b.id], b, frame_id)
            for b in bboxes if clusters[b.id] is not None]

        sub_metadata = self.filterMetadata(metadata)

        # TODO: update this portion
        # clusters = self.clusterByBBox(pcl, bboxes) # remove threshold here
        # metadata = [self.computeClusterMetadata(c, bboxes[i])
        #     for i, c in enumerate(clusters.values()) if c is not None]
        # # subselect metatada here + remove  def subselect metadata

        self.saveClusterMetadata(sub_metadata, frame.context.name)
        self.logger.debug('Exit:parseFrame')
        return

    def checkDataFile(self, frame):
        """Check if data file currently exists.

        Args:
            frame: waymo dataset Frame object whose context name is used to
                create filename for which to check

        """
        self.logger.debug('Entr:checkDataFile')
        file = '%s/%s.json' % (self.dir_save, frame.context.name)
        self.logger.debug('Show:frame_file=%s' % file)
        self.logger.debug('Exit:checkDataFile')
        return os.path.isfile(file)

    def run(self, data_file, file_number='', overwrite=False):
        """Generate data for all scans in all .tfrecord files in dir.

        Args:
            data_file: str .tfrecord file to parse
            file_number: optional str to print
            overwrite: Bool for overwriting already existing data

        Todo:
            put glob + directory stuff here

        """
        self.logger.debug('Entr:run')
        tfrecord = tf.data.TFRecordDataset(data_file, compression_type='')
        record_len = sum(1 for _ in tf.python_io.tf_record_iterator(data_file))
        self.logger.debug('Found %s frames in tfrecord' % record_len)

        progress = []  # Store progress shown to avoid rounding duplicates
        for i, scan in enumerate(tfrecord):

            # Transform raw waymo scan to numpy frame
            frame = self.waymo_converter.create_frame(scan)
            frame.context.name = '%s-%i' % (frame.context.name, i)
            print(frame.context.name)

            # Print percent complete
            percent = int(100 * i / record_len)
            if percent % 10 == 0 and percent not in progress:
                progress.append(percent)
                self.logger.info(
                    'STATUS UPDATE: tfrecord %s parse is %i%% percent complete.'
                    % (file_number, percent))

            # Parse frame if relevant json file doesn't already exist
            if self.checkDataFile(frame) and not overwrite:
                self.logger.info(
                    'frame %i is already parsed.' % i)
            else:
                self.logger.info(
                    'frame #: %i, tfrecord id: %s' \
                    % (i, str(frame.context.name)))
                self.parseFrame(frame, i)

        self.logger.info(
            'STATUS UPDATE: tfrecord parse is 100% percent complete.')
        self.logger.debug('Exit:run')
        return


class DatasetCreatorVis(DatasetCreator):
    """Class for visualizing DatasetCreator tasks with rviz."""

    def __init__(
            self, dir_load, dir_save, logger=None, dir_log=None,
            verbosity=None, visualize=0):
        """Initialize Ros components, DatasetCreator, visualize setting."""

        self.visualize = visualize
        self.ros_converter = Waymo2Ros()
        rp.init_node('dataset_creator_vis', disable_signals=True)
        self.marker_pub = rp.Publisher('/bboxes', MarkerArray, queue_size=1)
        self.pcl_pub = rp.Publisher('/pcl', PointCloud2, queue_size=1)
        DatasetCreator.__init__(
            self, dir_load=dir_load, dir_save=dir_save, logger=logger,
            dir_log=dir_log, verbosity=verbosity)
        self.logger.debug('Exit:__init__')

    def parseFrame(self, frame, frame_id):
        """Extract and save data from a single given frame, viz if specified.

        If self.visualize is 1, shows original data.
        If self.visualize is 2, shows ground filtered data.
        If self.visualize is 3, shows clustered data.
        If self.visualize is 4, shows density filtered data.

        Args:
            frame: waymo open dataset Frame with loaded data

        """
        self.logger.debug('Entr:parseFrame')
        pcl, bboxes = self.waymo_converter.unpack_frame(frame)
        # Update visualize param
        self.visualize = int(rp.get_param("/visualize", self.visualize))

        if self.visualize == 1:
            self.pcl_pub.publish(
                self.ros_converter.convert2pcl(pcl))
            self.marker_pub.publish(
                self.ros_converter.convert2markerarray(bboxes))

        pcl = self.filterPcl(pcl)

        if self.visualize == 2:
            self.pcl_pub.publish(
                self.ros_converter.convert2pcl(pcl))
            self.marker_pub.publish(
                self.ros_converter.convert2markerarray(bboxes))

        clusters = self.clusterByBBox(pcl, bboxes)
        valid_clusters = {k:v for k, v in clusters.iteritems() if v is not None}

        valid_clusters = {}
        valid_bboxes = []
        for bbox in bboxes:
            if clusters[bbox.id] is not None:
                valid_clusters[bbox.id] = clusters[bbox.id]
                valid_bboxes.append(bbox)

        if self.visualize == 3:
            try:
                self.pcl_pub.publish(self.ros_converter.convert2pcl(
                    np.concatenate(valid_clusters.values())))
                self.marker_pub.publish(self.ros_converter.convert2markerarray(
                    [b for b in bboxes if str(b.id) in valid_clusters.keys()]))
            except:
                self.logger.warning("No pcl with count > 10 pts")

        metadata = [self.computeClusterMetadata(valid_clusters[bbox.id], bbox,
            frame_id) for i, bbox in enumerate(valid_bboxes)]

        # TODO: Why do these two lists not contain the same IDs?
        # print("valid cluster ids:" )
        # print(valid_clusters.keys())
        # print("metadata cluster ids:")
        # for m in metadata:
        #     print(m.cluster_id)
        #
        # print("Total valid clusters: " + str(len(valid_clusters.keys())))
        # print("Total metadata clusters: " + str(len(metadata)))


        sub_metadata, sub_clusters = \
            self.filterMetadata(metadata, valid_clusters)

        if self.visualize == 4:
            # try:
                # print(sub_clusters)
            self.pcl_pub.publish(self.ros_converter.convert2pcl(
                np.concatenate(sub_clusters.values())))
            self.marker_pub.publish(self.ros_converter.convert2markerarray(
                [b for b in bboxes if str(b.id) in sub_clusters.keys()]))
            # except:
                # self.logger.warning("No pcl with density > 100 pts/m^3")


        self.saveClusterMetadata(sub_metadata, frame.context.name)
        self.logger.debug('Exit:parseFrame')
        return


if __name__ == "__main__":
    """Set up directory locations and create dataset."""

    enable_rviz = rp.get_param("/enable_rviz", False)

    user = 'amy'
    loc_pkg = '/home/amy/test_ws/src/ml_person_detection'
    dataset = 'training_0000'

    args_default = {
        'dir_load' : '/home/%s/test_ws/src/waymo-od/data' % (user),
        'dir_log' : '/home/%s/test_ws/src/waymo-od/logs' % (user),
        'dir_save' : '/home/%s/test_ws/src/waymo-od/save' % (user),
        'verbosity' : logging.DEBUG,
        'visualize' : 2
    }

    if enable_rviz:
        dir_load = rp.get_param("/dir_load", args_default['dir_load'])
        dir_log = rp.get_param("/dir_log", args_default['dir_log'])
        dir_save = rp.get_param("/dir_save", args_default['dir_save'])
        verbosity = int(rp.get_param("/verbosity", args_default['verbosity']))
        visualize = int(rp.get_param("/visualize", args_default['visualize']))

        creator = DatasetCreatorVis(
            dir_load=dir_load, dir_save=dir_save, dir_log=dir_log,
            verbosity=verbosity, visualize=visualize)

    else:
        dir_load = args_default['dir_load']
        dir_log = args_default['dir_log']
        dir_save = args_default['dir_save']
        verbosity = args_default['verbosity']
        visualize = args_default['visualize']

        creator = DatasetCreator(
            dir_load=dir_load, dir_save=dir_save, dir_log=dir_log,
            verbosity=verbosity)

    creator.logger.info("enable_rviz = %s" % enable_rviz)

    file_list = glob.glob('%s/*.tfrecord' % dir_load)
    tfrecord_len = sum(1 for _ in file_list)
    creator.logger.info(
        'Found %i tfrecord files in dataset %s' % (tfrecord_len, dataset))

    for i, f in enumerate(file_list):

        # Print percent complete
        creator.logger.info(
            'STATUS UPDATE: dataset parse is %i%% percent complete.'
            % int(100 * i / tfrecord_len))

        creator.run(f, i)
