# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import torch

from maskrcnn_benchmark.modeling.box_coder import BoxCoder
from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist
from maskrcnn_benchmark.structures.boxlist_ops import boxlist_nms
from maskrcnn_benchmark.structures.boxlist_ops import remove_small_boxes

from ..utils import cat
from .utils import permute_and_flatten


class RPNPostProcessor(torch.nn.Module):
    """
    Performs post-processing on the outputs of the RPN boxes, before feeding the
    proposals to the heads
    """

    def __init__(
            self,
            pre_nms_top_n,
            post_nms_top_n,
            nms_thresh,
            min_size,
            box_coder=None,
            fpn_post_nms_top_n=None,
            fpn_post_nms_per_batch=True,
    ):
        """
        Arguments:
            pre_nms_top_n (int)
            post_nms_top_n (int)
            nms_thresh (float)
            min_size (int)
            box_coder (BoxCoder)
            fpn_post_nms_top_n (int)
            常用的参数有：pre_nms_top_n为每一个特征层上选取的边框数；
            post_nms_top_n为在得到的所有层边框中进一步选择的边框数；
            nms_thresh为非极大线性抑制（NMS）的阈值；
            min_size为所能接受的最小的边框大小；
        """

        super(RPNPostProcessor, self).__init__()
        self.pre_nms_top_n = pre_nms_top_n
        self.post_nms_top_n = post_nms_top_n
        self.nms_thresh = nms_thresh
        self.min_size = min_size

        if box_coder is None:
            box_coder = BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))
        self.box_coder = box_coder

        if fpn_post_nms_top_n is None:
            fpn_post_nms_top_n = post_nms_top_n
        self.fpn_post_nms_top_n = fpn_post_nms_top_n
        self.fpn_post_nms_per_batch = fpn_post_nms_per_batch

    def add_gt_proposals(self, proposals, targets):
        """
        Arguments:
            proposals: list[BoxList]
            targets: list[BoxList]
        """
        # Get the device we're operating on
        device = proposals[0].bbox.device

        gt_boxes = [target.copy_with_fields([]) for target in targets]

        # later cat of bbox requires all fields to be present for all bbox
        # so we need to add a dummy for objectness that's missing
        for gt_box in gt_boxes:
            gt_box.add_field("objectness", torch.ones(len(gt_box), device=device))

        proposals = [
            cat_boxlist((proposal, gt_box))
            for proposal, gt_box in zip(proposals, gt_boxes)
        ]

        return proposals

    def forward_for_single_feature_map(self, anchors, objectness, box_regression):
        """
        Arguments:
            anchors: list[BoxList]
            objectness: tensor of size N, A, H, W
            得到Ｎ=图片数（batch）,A=ratio数,H=该层特征图高,W=该层特征图宽

            box_regression: tensor of size N, A * 4, H, W
        """
        device = objectness.device
        N, A, H, W = objectness.shape

        # put in the same format as anchors
        # 在得到的目标特征图上扩充一维,该维度为特定特征图的某一个位置上anchor内是否有目标
        # 然后取消掉除FPN层数以外的所有维度，合并到一个维度上，将图片数，高，宽等信息压缩为一维
        objectness = permute_and_flatten(objectness, N, A, 1, H, W).view(N, -1)
        # 输出N张图，-1个待回归框，每个框需要1个得分值
        objectness = objectness.sigmoid()

        # 在得到的目标特征图上扩充一维,该维度为特定特征图的某一个位置上anchor的边框信息。
        box_regression = permute_and_flatten(box_regression, N, A, 4, H, W)
        # 输出N张图，-1个待回归框，每个框需要4个回归值

        num_anchors = A * H * W

        pre_nms_top_n = min(self.pre_nms_top_n, num_anchors)  # 得到在训练过程中设置的每张图片选取的anchor数（在每个特征图上）

        # 得到前pre_nms_top_n个目标评分最高的anchor的目标评分以及该anchor在anchor列表中的索引
        objectness, topk_idx = objectness.topk(pre_nms_top_n, dim=1, sorted=True)

        # 初始化图片个数的索引
        batch_idx = torch.arange(N, device=device)[:, None]

        # 得到前pre_nms_top_n个目标评分最高的anchor的边框回归信息！！！
        box_regression = box_regression[batch_idx, topk_idx]

        # 获取图片尺寸信息
        image_shapes = [box.size for box in anchors]
        concat_anchors = torch.cat([a.bbox for a in anchors], dim=0)

        # 得到pre_nms_top_n个目标评分最高的anchor信息！！！
        concat_anchors = concat_anchors.reshape(N, -1, 4)[batch_idx, topk_idx]

        # 利用anchor坐标和回归信息，得到proposal边框
        proposals = self.box_coder.decode(
            box_regression.view(-1, 4), concat_anchors.view(-1, 4)  # rpn输出的是'xyxy'格式的
        )  # 用实际xyxy坐标和回归值就能得到新的检测框

        proposals = proposals.view(N, -1, 4)

        result = []
        for proposal, score, im_shape in zip(proposals, objectness, image_shapes):
            # 将预测边框保存到BoxList，
            # 为每一个FPN层的每一张图的所有候选框建立一个BoxList
            boxlist = BoxList(proposal, im_shape, mode="xyxy")
            boxlist.add_field("objectness", score)  # 将每个anchor的目标评分保存到BoxList
            boxlist = boxlist.clip_to_image(remove_empty=False)
            boxlist = remove_small_boxes(boxlist, self.min_size)
            boxlist = boxlist_nms(
                boxlist,
                self.nms_thresh,
                max_proposals=self.post_nms_top_n,
                score_field="objectness",
            )
            result.append(boxlist)
        return result  # rpn输出的是'xyxy'格式的

    def forward(self, anchors, objectness, box_regression, targets=None):
        """
        Arguments:
            anchors: list[list[BoxList]]
            objectness: list[tensor]
            box_regression: list[tensor]
         参数:
            anchors: FPN层数×　图片数（batch） × 包含每个图片在每一个FPN输出层上生成的anchor
            objectness: FPN层数 ×　[图片数（batch）×　ratio个数　×　该层特征图宽　×　该层特征图高]；
            就是RPNHead提取的类别信息
            box_regression: FPN层数　×　[图片数（batch）×　４倍ratio个数　×　该层特征图宽　×　该层特征图高]；
            就是RPNHead提取的边框回归信息

        Returns:
            boxlists (list[BoxList]): the post-processed anchors, after
                applying box decoding and NMS
            经过边框解码和NMS处理后剩下的anchor
        """
        sampled_boxes = []
        num_levels = len(objectness)
        anchors = list(zip(*anchors))

        # 得到FPN每一个特征层上的预测边框列表，数据格式为：FPN层数×图片数×预测边框
        for a, o, b in zip(anchors, objectness, box_regression):
            sampled_boxes.append(self.forward_for_single_feature_map(a, o, b))

        boxlists = list(zip(*sampled_boxes))
        boxlists = [cat_boxlist(boxlist) for boxlist in boxlists]

        if num_levels > 1:  # 当FPN有多层时
            boxlists = self.select_over_all_levels(boxlists)

        # append ground-truth bboxes to proposals把基准边框也添加到预测边框里
        if self.training and targets is not None:
            boxlists = self.add_gt_proposals(boxlists, targets)

        return boxlists

    def select_over_all_levels(self, boxlists):
        num_images = len(boxlists)
        # different behavior during training and during testing:
        # during training, post_nms_top_n is over *all* the proposals combined, while
        # during testing, it is over the proposals for each image
        # NOTE: it should be per image, and not per batch. However, to be consistent 
        # with Detectron, the default is per batch (see Issue #672)
        if self.training and self.fpn_post_nms_per_batch:
            objectness = torch.cat(
                [boxlist.get_field("objectness") for boxlist in boxlists], dim=0
            )
            box_sizes = [len(boxlist) for boxlist in boxlists]
            post_nms_top_n = min(self.fpn_post_nms_top_n, len(objectness))
            _, inds_sorted = torch.topk(objectness, post_nms_top_n, dim=0, sorted=True)
            inds_mask = torch.zeros_like(objectness, dtype=torch.bool)
            inds_mask[inds_sorted] = 1
            inds_mask = inds_mask.split(box_sizes)
            for i in range(num_images):
                boxlists[i] = boxlists[i][inds_mask[i]]
        else:
            for i in range(num_images):
                objectness = boxlists[i].get_field("objectness")
                post_nms_top_n = min(self.fpn_post_nms_top_n, len(objectness))
                _, inds_sorted = torch.topk(
                    objectness, post_nms_top_n, dim=0, sorted=True
                )
                boxlists[i] = boxlists[i][inds_sorted]
        return boxlists


def make_rpn_postprocessor(config, rpn_box_coder, is_train):
    fpn_post_nms_top_n = config.MODEL.RPN.FPN_POST_NMS_TOP_N_TRAIN  # 2000
    if not is_train:
        fpn_post_nms_top_n = config.MODEL.RPN.FPN_POST_NMS_TOP_N_TEST

    pre_nms_top_n = config.MODEL.RPN.PRE_NMS_TOP_N_TRAIN  # 2000，When FPN is used, this is *per FPN level* (not total)
    post_nms_top_n = config.MODEL.RPN.POST_NMS_TOP_N_TRAIN  # 2000
    if not is_train:
        pre_nms_top_n = config.MODEL.RPN.PRE_NMS_TOP_N_TEST
        post_nms_top_n = config.MODEL.RPN.POST_NMS_TOP_N_TEST
    fpn_post_nms_per_batch = config.MODEL.RPN.FPN_POST_NMS_PER_BATCH  # True
    nms_thresh = config.MODEL.RPN.NMS_THRESH  # 0.7
    min_size = config.MODEL.RPN.MIN_SIZE  # 0
    box_selector = RPNPostProcessor(
        pre_nms_top_n=pre_nms_top_n,
        post_nms_top_n=post_nms_top_n,
        nms_thresh=nms_thresh,
        min_size=min_size,
        box_coder=rpn_box_coder,
        fpn_post_nms_top_n=fpn_post_nms_top_n,
        fpn_post_nms_per_batch=fpn_post_nms_per_batch,
    )
    return box_selector
