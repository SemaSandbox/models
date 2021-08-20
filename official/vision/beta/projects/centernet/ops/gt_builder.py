# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Dict

import tensorflow as tf

from official.vision.beta.projects.centernet.ops import preprocess_ops


def build_heatmap_and_regressed_features(labels: Dict,
                                         output_size: List[int],
                                         input_size: List[int],
                                         num_classes: int = 90,
                                         max_num_instances: int = 128,
                                         use_gaussian_bump: bool = True,
                                         gaussian_rad: int = -1,
                                         gaussian_iou: float = 0.7,
                                         class_offset: int = 0,
                                         dtype='float32'):
  """ Generates the ground truth labels for centernet.
  
  Ground truth labels are generated by splatting gaussians on heatmaps for
  corners and centers. Regressed features (offsets and sizes) are also
  generated.

  Args:
    labels: A dictionary of COCO ground truth labels with at minimum the
      following fields:
      bbox: A `Tensor` of shape [max_num_instances, 4], where the
        last dimension corresponds to the top left x, top left y, bottom right x,
        and bottom left y coordinates of the bounding box
      classes: A `Tensor` of shape [max_num_instances] that contains
        the class of each box, given in the same order as the boxes
      num_detections: A `Tensor` or int that gives the number of objects
    output_size: A `list` of length 2 containing the desired output height
      and width of the heatmaps
    input_size: A `list` of length 2 the expected input height and width of
      the image
    num_classes: A `Tensor` or `int` for the number of classes.
    max_num_instances: An `int` number of maximum number of instances in an image.
    use_gaussian_bump: A `boolean` indicating whether or not to splat a
      gaussian onto the heatmaps. If set to False, a value of 1 is placed at
      the would-be center of the gaussian.
    gaussian_rad: A `int` for the desired radius of the gaussian. If this
      value is set to -1, then the radius is computed using gaussian_iou.
    gaussian_iou: A `float` number for the minimum desired IOU used when
      determining the gaussian radius of center locations in the heatmap.
    class_offset: A `int` for subtracting a value from the ground truth classes
    dtype: `str`, data type. One of {`bfloat16`, `float32`, `float16`}.
  
  Returns:
    Dictionary of labels with the following fields:
      'ct_heatmaps': Tensor of shape [output_h, output_w, num_classes],
        heatmap with splatted gaussians centered at the positions and channels
        corresponding to the center location and class of the object
      'ct_offset': `Tensor` of shape [max_num_instances, 2], where the first
        num_boxes entries contain the x-offset and y-offset of the center of
        an object. All other entires are 0
      'size': `Tensor` of shape [max_num_instances, 2], where the first
        num_boxes entries contain the width and height of an object. All
        other entires are 0
      'box_mask': `Tensor` of shape [max_num_instances], where the first
        num_boxes entries are 1. All other entires are 0
      'box_indices': `Tensor` of shape [max_num_instances, 2], where the first
        num_boxes entries contain the y-center and x-center of a valid box.
        These are used to extract the regressed box features from the
        prediction when computing the loss
    """
  if dtype == 'float16':
    dtype = tf.float16
  elif dtype == 'bfloat16':
    dtype = tf.bfloat16
  elif dtype == 'float32':
    dtype = tf.float32
  else:
    raise Exception(
        'Unsupported datatype used in ground truth builder only '
        '{float16, bfloat16, or float32}')
  
  # Get relevant bounding box and class information from labels
  # only keep the first num_objects boxes and classes
  num_objects = labels['num_detections']
  # shape of labels['bbox'] is [max_num_instances, 4]
  # [ymin, xmin, ymax, xmax]
  boxes = tf.cast(labels['bbox'], dtype)[:num_objects]
  # shape of labels['classes'] is [max_num_instances, ]
  classes = tf.cast(labels['classes'] - class_offset, dtype)[:num_objects]
  
  # Compute scaling factors for center/corner positions on heatmap
  input_size = tf.cast(input_size, dtype)
  output_size = tf.cast(output_size, dtype)
  input_h, input_w = input_size[0], input_size[1]
  output_h, output_w = output_size[0], output_size[1]
  
  width_ratio = output_w / input_w
  height_ratio = output_h / input_h
  
  # Original box coordinates
  # [num_objects, ]
  ytl, ybr = boxes[..., 0], boxes[..., 2]
  xtl, xbr = boxes[..., 1], boxes[..., 3]
  yct = (ytl + ybr) / 2
  xct = (xtl + xbr) / 2
  
  # Scaled box coordinates (could be floating point)
  # [num_objects, ]
  scale_xct = xct * width_ratio
  scale_yct = yct * height_ratio
  
  # Floor the scaled box coordinates to be placed on heatmaps
  # [num_objects, ]
  scale_xct_floor = tf.math.floor(scale_xct)
  scale_yct_floor = tf.math.floor(scale_yct)
  
  # Offset computations to make up for discretization error
  # used for offset maps
  # [num_objects, 2]
  ct_offset_values = tf.stack([scale_yct - scale_yct_floor,
                               scale_xct - scale_xct_floor], axis=-1)
  
  # Get the scaled box dimensions for computing the gaussian radius
  # [num_objects, ]
  box_widths = boxes[..., 3] - boxes[..., 1]
  box_heights = boxes[..., 2] - boxes[..., 0]
  
  box_widths = box_widths * width_ratio
  box_heights = box_heights * height_ratio
  
  # Used for size map
  # [num_objects, 2]
  box_heights_widths = tf.stack([box_heights, box_widths], axis=-1)
  
  # Center/corner heatmaps
  # [output_h, output_w, num_classes]
  ct_heatmap = tf.zeros((output_h, output_w, num_classes), dtype)
  
  # Maps for offset and size features for each instance of a box
  # [max_num_instances, 2]
  ct_offset = tf.zeros((max_num_instances, 2), dtype)
  # [max_num_instances, 2]
  size = tf.zeros((max_num_instances, 2), dtype)
  
  # Mask for valid box instances and their center indices in the heatmap
  # [max_num_instances, ]
  box_mask = tf.zeros((max_num_instances), tf.int32)
  # [max_num_instances, 2]
  box_indices = tf.zeros((max_num_instances, 2), tf.int32)
  
  if num_objects > 0:
    if use_gaussian_bump:
      # Need to gaussians around the centers and corners of the objects
      
      # First compute the desired gaussian radius
      if gaussian_rad == -1:
        radius = tf.map_fn(
            fn=lambda x: preprocess_ops.gaussian_radius(x, gaussian_iou),
            elems=tf.math.ceil(box_heights_widths))
        radius = tf.math.maximum(tf.math.floor(radius),
                                 tf.cast(1.0, radius.dtype))
      else:
        radius = tf.constant([gaussian_rad] * max_num_instances, dtype)
        radius = radius[:num_objects]
      # These blobs contain information needed to draw the gaussian
      ct_blobs = tf.stack([classes, scale_yct_floor, scale_xct_floor, radius],
                          axis=-1)
      
      # Get individual gaussian contributions from each bounding box
      ct_gaussians = tf.map_fn(
          fn=lambda x: preprocess_ops.draw_gaussian(
              tf.shape(ct_heatmap), x, dtype),
          elems=ct_blobs)
      
      # Combine contributions into single heatmaps
      ct_heatmap = tf.math.reduce_max(ct_gaussians, axis=0)
    
    else:
      # Instead of a gaussian, insert 1s in the center and corner heatmaps
      # [num_objects, 3]
      ct_hm_update_indices = tf.cast(
          tf.stack([scale_yct_floor, scale_xct_floor, classes], axis=-1),
          tf.int32)
      
      ct_heatmap = tf.tensor_scatter_nd_update(ct_heatmap,
                                               ct_hm_update_indices,
                                               [1] * num_objects)
  
    # Indices used to update offsets and sizes for valid box instances
    update_indices = preprocess_ops.cartesian_product(
        tf.range(num_objects), tf.range(2))
    # [num_objects, 2, 2]
    update_indices = tf.reshape(update_indices, shape=[num_objects, 2, 2])
    
    # Write the offsets of each box instance
    ct_offset = tf.tensor_scatter_nd_update(
        ct_offset, update_indices, ct_offset_values)
    
    # Write the size of each bounding box
    size = tf.tensor_scatter_nd_update(
        size, update_indices, box_heights_widths)
    
    # Initially the mask is zeros, so now we unmask each valid box instance
    mask_indices = tf.expand_dims(tf.range(num_objects), -1)
    mask_values = tf.repeat(1, num_objects)
    box_mask = tf.tensor_scatter_nd_update(box_mask, mask_indices, mask_values)
    
    # Write the y and x coordinate of each box center in the heatmap
    box_index_values = tf.cast(
        tf.stack([scale_yct_floor, scale_xct_floor], axis=-1),
        dtype=tf.int32)
    box_indices = tf.tensor_scatter_nd_update(
        box_indices, update_indices, box_index_values)
  labels = {
      # [output_h, output_w, num_classes]
      'ct_heatmaps': ct_heatmap,
      # [max_num_instances, 2]
      'ct_offset': ct_offset,
      # [max_num_instances, 2]
      'size': size,
      # [max_num_instances, ]
      'box_mask': box_mask,
      # [max_num_instances, 2]
      'box_indices': box_indices
  }
  return labels


if __name__ == '__main__':
  import time
  
  boxes = tf.constant([
      (10, 300, 15, 370),  # center (y, x) = (12, 335)
      (100, 300, 150, 370),  # center (y, x) = (125, 335)
      (15, 100, 200, 170),  # center (y, x) = (107, 135)
  ], dtype=tf.float32)
  
  classes = tf.constant((1, 1, 1), dtype=tf.float32)
  
  boxes = preprocess_ops.pad_max_instances(boxes, 128, 0)
  classes = preprocess_ops.pad_max_instances(classes, 128, -1)
  
  boxes = tf.stack([boxes, boxes], axis=0)
  classes = tf.stack([classes, classes], axis=0)
  
  print('boxes shape:', boxes.get_shape())
  print('classes shape:', classes.get_shape())
  
  print("testing new build heatmaps function: ")
  a = time.time()
  
  gt_label = tf.map_fn(
      fn=lambda x: build_heatmap_and_regressed_features(
          labels=x,
          output_size=[128, 128],
          input_size=[512, 512]),
      elems={
          'bbox': boxes,
          'num_detections': tf.constant([3, 3]),
          'classes': classes
      },
      dtype={
          'ct_heatmaps': tf.float32,
          'ct_offset': tf.float32,
          'size': tf.float32,
          'box_mask': tf.int32,
          'box_indices': tf.int32
      }
  )
  b = time.time()
  for item in gt_label:
    print(item, gt_label[item].shape)
  print("Time taken: {} ms".format((b - a) * 1000))
