from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from net.Snet49 import ShuffleNetV2
from net.roi_layers.ps_roi_align import PSRoIAlign
from net.bbox_tools import generate_anchors

from net.rpn import AnchorGenerator
from net.rpn import RegionProposalNetwork
from net.rpn import RPNHead
from net.roi_layers.poolers import MultiScaleRoIAlign
from net.generalized_rcnn import GeneralizedRCNN

from torchvision.models.detection.roi_heads import RoIHeads
from torchvision.models.detection.transform import GeneralizedRCNNTransform

class CEM(nn.Module):
    def __init__(self):
        super(CEM, self).__init__()
        self.conv1 = nn.Conv2d(120, 245, kernel_size=1, stride=1, padding=0)

        self.conv2 = nn.Conv2d(512, 245, kernel_size=1, stride=1, padding=0)

        self.avg_pool = nn.AvgPool2d(10)
        self.conv3 = nn.Conv2d(512, 245, kernel_size=1, stride=1, padding=0)

    def forward(self, c4_feature, c5_feature):
        # c4
        c4 = c4_feature
        c4_lat = self.conv1(c4)             # output: [245, 20, 20]

        # c5
        c5 = c5_feature
        c5_lat = self.conv2(c5)             # output: [245, 10, 10]
        # upsample x2
        c5_lat = F.interpolate(input=c5_lat, size=[20, 20], mode="nearest") # output: [245, 20, 20]

        c_glb = self.avg_pool(c5)           # output: [512, 1, 1]
        c_glb_lat = self.conv3(c_glb)       # output: [245, 1, 1]

        out = c4_lat + c5_lat + c_glb_lat   # output: [245, 20, 20]
        return out

class SAM(nn.Module):
    def __init__(self):
        super(SAM, self).__init__()
        self.conv = nn.Conv2d(256, 245, 1, 1, 0, bias=False) # input channel = 245 ?
        self.bn = nn.BatchNorm2d(245)
        self.sigmoid = nn.Sigmoid()
        
    def forward(self, rpn_feature, cem_feature):
        cem = cem_feature      # feature map of CEM: [245, 20, 20]
        rpn = rpn_feature      # feature map of RPN: [256, 20, 20]

        sam = self.conv(rpn)
        sam = self.bn(sam)
        sam = self.sigmoid(sam)
        out = cem * sam     # output: [245, 20, 20]

        return out

class RPN(nn.Module):
    def __init__(self):
        super(RPN, self).__init__()
        # RPN
        self.dw5x5 = nn.Conv2d(245, 245, kernel_size=5, stride=1, padding=2, groups=245, bias=False)
        self.bn0 = nn.BatchNorm2d(245)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(245, 256, kernel_size=1, stride=1, padding=0)
        self.bn1 = nn.BatchNorm2d(256)
        #self.conv2 = nn.Conv2d(num_anchors, (1, 1))         # class
        #self.conv3 = nn.Conv2d(num_anchors * 4, (1, 1))     # region

        anchor_generator = generate_anchors()

    def forward(self, x):   # x: CEM output feature (20x20x245)
        # RPN
        x = self.dw5x5(x)   # output: [245, 20, 20]
        x = self.bn0(x)
        x = self.relu(x)
        x = self.conv1(x)   # output: [256, 20, 20]
        x = self.bn1(x)
        x = self.relu(x)
        return x

class RCNN_Subnet(nn.Module):
    def __init__(self, nb_classes):
        super(RCNN_Subnet, self).__init__()
        self.linear = nn.Linear(245, 1024)       # fc

        # classification
        self.linear_cls = nn.Linear(1024, nb_classes)
        self.softmax = nn.Softmax(dim=0) 

        # localization
        self.linear_reg = nn.Linear(1024, 4 * (nb_classes - 1))

    def forward(self, x):       # x: 7x7x5 
        x = torch.flatten(x)
        out = self.linear(x)                    # output: [1, 1024]

        # classification
        out_score = self.linear_cls(out)        # output: [nb_classes]
        out_class = self.softmax(out_score)

        # localization
        out_regressor = self.linear_reg(out)

        return [out_class, out_regressor]              
        

def detecter():
    img = torch.randn(1, 3, 320, 320)

    snet = ShuffleNetV2()
    snet_feature, c4_feature, c5_feature = snet(img)

    cem = CEM()
    #cem_input = [c4_feature, c5_feature] # c4: [120, 20, 20]  c5: [512, 10, 10]
    cem_output = cem(c4_feature, c5_feature)          # output: [245, 20, 20]     

    rpn = RPN()
    rpn_output = rpn(cem_output)            # output: [256, 20, 20]

    sam = SAM()
    sam_input = [cem_output, rpn_output]
    sam_output = sam(sam_input)             # output: [245, 20, 20]

    # PS ROI Align
    roi_regions = 7
    # (Tensor[K, 5] or List[Tensor[L, 4]]): the box coordinates in (x1, y1, x2, y2)
    ps_roi_align = PSRoIAlign(output_size=[roi_regions, roi_regions], spatial_scale=1.0, sampling_ratio=-1)
    #ps_roi_align_output = ps_roi_align(input=sam_output, rois=input_rois)

    
    feature_roi = torch.randn(1, 5, 7, 7)
    nb_classes = 80
    rcnn = RCNN_Subnet(nb_classes)
    #rcnn_output = rcnn(ps_roi_align_output)
    rcnn_output = rcnn(feature_roi)
    

class DetectNet(GeneralizedRCNN):
    def __init__(self, backbone, num_classes=None,
        # transform parameters
        min_size=800, max_size=1333,
        image_mean=None, image_std=None,
        # RPN parameters
        rpn_anchor_generator=None, rpn_head=None,
        rpn_pre_nms_top_n_train=2000, rpn_pre_nms_top_n_test=100,
        rpn_post_nms_top_n_train=2000, rpn_post_nms_top_n_test=1000,

        rpn_mns_thresh=0.7,
        rpn_fg_iou_thresh=0.7, rpn_bg_iou_thresh=0.3,
        rpn_batch_size_per_image=256, rpn_positive_fraction=0.5,

        # Box parameters
        box_ps_roi_align=None, box_head=None, box_predictor=None,
        box_score_thresh=0.05, box_nms_thresh=0.5, box_detections_per_img=100,
        box_fg_iou_thresh=0.5,box_bg_iou_thresh=0.5,
        box_batch_size_per_image=512, box_positive_fraction=0.25,
        bbox_reg_weights=None):

        if not hasattr(backbone, "out_channels"):
            raise ValueError(
                "backbone should contain an attribute out_channels "
                "specifying the number of output channels (assumed to be the "
                "same for all the levels)")

        assert isinstance(rpn_anchor_generator, (AnchorGenerator, type(None)))
        assert isinstance(box_ps_roi_align, (MultiScaleRoIAlign, type(None)))

        if num_classes is not None:
            if box_predictor is not None:
                raise ValueError("num_classes should be None when box_predictor is specified")
        else:
            if box_predictor is None:
                raise ValueError("num_classes should not be None when box_predictor "
                                 "is not specified")

        out_channels = backbone.out_channels    # 245

        # CEM module
        cem = CEM() 

        # SAM module
        sam = SAM()

        # rpn
        if rpn_anchor_generator is None:
            anchor_sizes = ((32,), (64,), (128,), (256,), (512,))
            aspect_ratios = ((0.5, 1.0, 2.0),) * len(anchor_sizes)
            rpn_anchor_generator = AnchorGenerator(sizes=anchor_sizes, aspect_ratios=aspect_ratios)
        
        if rpn_head is None:
            rpn_head = RPNHead(
                out_channels, rpn_anchor_generator.num_anchors_per_location()[0]
            )

        rpn_pre_nms_top_n = dict(training=rpn_pre_nms_top_n_train, testing=rpn_pre_nms_top_n_test)
        rpn_post_nms_top_n = dict(training=rpn_post_nms_top_n_train, testing=rpn_post_nms_top_n_test)

        rpn = RegionProposalNetwork(
            rpn_anchor_generator, rpn_head,
            rpn_fg_iou_thresh, rpn_bg_iou_thresh,
            rpn_batch_size_per_image, rpn_positive_fraction,
            rpn_pre_nms_top_n, rpn_post_nms_top_n, rpn_mns_thresh)


        # ps roi align
        if box_ps_roi_align is None:
            box_ps_roi_align = MultiScaleRoIAlign(          # ps roi align
                featmap_names=['0', '1', '2', '3'],
                output_size=7,
                sampling_ratio=2)

        # R-CNN subnet
        if box_head is None:
            resolution = box_ps_roi_align.output_size[0]    # size: (7, 7)
            representation_size = 1024
            box_out_channels = 5
            box_head = RCNNSubNetHead(
                box_out_channels * resolution ** 2,         # 5 * 7 * 7
                representation_size)

        if box_predictor is None:
            representation_size = 1024
            box_predictor = ThunderNetPredictor(
                representation_size,
                num_classes)

        roi_heads = RoIHeads(
            # Box
            box_ps_roi_align, box_head, box_predictor,
            box_fg_iou_thresh, box_bg_iou_thresh,
            box_batch_size_per_image, box_positive_fraction,
            bbox_reg_weights,
            box_score_thresh, box_nms_thresh, box_detections_per_img)


        if image_mean is None:
            image_mean = [0.485, 0.456, 0.406]
        if image_std is None:
            image_std = [0.229, 0.224, 0.225]
        transform = GeneralizedRCNNTransform(min_size, max_size, image_mean, image_std)

        super(DetectNet, self).__init__(backbone, cem, sam, rpn, roi_heads, transform)

class RCNNSubNetHead(nn.Module):
    """
    Standard heads for FPN-based models
    Arguments:
        in_channels (int): number of input channels
        representation_size (int): size of the intermediate representation
    """

    def __init__(self, in_channels, representation_size):
        super(RCNNSubNetHead, self).__init__()
        self.fc6 = nn.Linear(in_channels, representation_size)  # in_channles: 7*7*5=245  representation_size:1024

    def forward(self, x):
        x = x.flatten(start_dim=1)

        x = F.relu(self.fc6(x))

        return x
  
class ThunderNetPredictor(nn.Module):
    """
    Standard classification + bounding box regression layers
    for Fast R-CNN.
    Arguments:
        in_channels (int): number of input channels
        num_classes (int): number of output classes (including background)
    """
    def __init__(self, in_channels, num_classes):
        super(ThunderNetPredictor, self).__init__()
        self.cls_score = nn.Linear(in_channels, num_classes)
        self.bbox_pred = nn.Linear(in_channels, num_classes * 4)

    def forward(self, x):       # x: [1024, 1, 1]
        if x.dim() == 4:
            assert list(x.shape[2:]) == [1, 1]
        x = x.flatten(start_dim=1)
        scores = self.cls_score(x)
        bbox_deltas = self.bbox_pred(x)

        return scores, bbox_deltas
        

def ThunderNet():
    snet = ShuffleNetV2()
    snet.out_channels = 245
    thundernet = DetectNet(snet, num_classes=2)

    return thundernet


#if __name__ == '__main__':
#    thundernet = ThunderNet()
#    print('thundernet: ', thundernet)
    
