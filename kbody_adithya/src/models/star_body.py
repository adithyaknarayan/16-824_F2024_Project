import torch
import numpy as np
import typing

#NOTE: code from https://github.com/ahmedosman/STAR

__all__ = ['STAR', 'SMPLX2STAR', 'ToOpenPose']

def passthrough(*args: typing.Any) -> typing.Any:
    return args[0] if len(args) == 1 else args

def _star_to_openpose(joints: torch.Tensor, vertices: torch.Tensor) -> torch.Tensor:
    # NECK#12 is in the middle of the shoulders (Left#16 and Right#17)
    # joints[:, 12, : ] = joints[:, 16:18, :].mean(dim=1)
    # NECK#12 is in the middle of the clavicles (Left#13 and Right#14)
    # joints[:, 12, : ] = joints[:, 13:15, :].mean(dim=1)
    joints[:, 12, : ] = joints[:, 12:15, :].mean(dim=1)
    # MIDHIP/PELVIS#0 is at the center of the hips (Left#1 and Right#2) and the Pelvis#0
    joints[:, 0, :] = joints[:, :3, :].mean(dim=1)
    # NOSE VERTEX := 331 instead of the HEAD
    # joints[:, 15, :] = vertices[:, 407, :] # vertices[:, 331, :] # or 407/409    
    joints[:, 15, :] = vertices[:, 331, :]
    return joints

def _extract_face(vertices: torch.Tensor) -> torch.Tensor:
    # RightEar  -> VID: 3969
    # LeftEar   -> VID: 480 or 220
    # RightEye  -> VID: 6244
    # LeftEye   -> VID: 2784
    return torch.cat([
        vertices[:, 3899:3899+1, :], # vertices[:, 3969:3969+1, :], # REar        
        vertices[:, 6244:6244+1, :], # REye
        vertices[:, 2784:2784+1, :], # LEye
        vertices[:, 220:220+1, :], # vertices[:, 480:480+1, :], # LEar
    ], dim=-2)

__JOINT_FORMAT_MAPS__ = {
    'star':         passthrough,
    'none':         passthrough,
    'identity':     passthrough,
    'openpose':     _star_to_openpose,
}

class STAR(torch.nn.Module):
    def __init__(self,
        model_path:         str='',
        num_betas:          int=10,
        joint_format:       str='openpose',
        append_face:        bool=False,
    ):
        super(STAR, self).__init__()
        #TODO: assert gender choices or unify with ckpt
        #TODO: assert file exists
        self.joint_mapper = __JOINT_FORMAT_MAPS__.get(joint_format, passthrough)
        star_model = np.load(model_path, allow_pickle=True)
        J_regressor = star_model['J_regressor']
        self.num_betas = num_betas
        self.append_face = append_face
        #NOTE: check if np arrays are doubles
        self.register_buffer('J_regressor', torch.Tensor(
            J_regressor
        ).float())
        self.register_buffer('weights', torch.Tensor(
            star_model['weights']).float()
        )
        self.register_buffer('posedirs', torch.Tensor(
            star_model['posedirs'].reshape((-1, 93))
        ).float())
        self.register_buffer('v_template', torch.Tensor(
            star_model['v_template']
        ).float())
        self.register_buffer('shapedirs', torch.from_numpy(
            np.array(star_model['shapedirs'][:,:,:num_betas])
        ).float())
        self.register_buffer('faces', torch.from_numpy(
            star_model['f'].astype(np.int64)
        ).unsqueeze(0))
        self.f = star_model['f'] #NOTE: is this needed?
        self.register_buffer('kintree_table', torch.from_numpy(
            star_model['kintree_table'].astype(np.int64))
        )
        id_to_col = {
            self.kintree_table[1, i].item(): i for i in range(self.kintree_table.shape[1])
        }
        self.register_buffer('parent', torch.Tensor(
            [id_to_col[self.kintree_table[0, it].item()] for it in range(1, self.kintree_table.shape[1])]
        ).long())

    def _quat_feat(self, theta: torch.Tensor) -> torch.Tensor:
        '''
            Computes a normalized quaternion ([0,0,0,0]  when the body is in rest pose)
            given joint angles
        :param theta: A tensor of joints axis angles, batch size x number of joints x 3
        :return:
        '''
        l1norm = torch.norm(theta + 1e-8, p=2, dim=1)
        angle = torch.unsqueeze(l1norm, -1)
        normalized = torch.div(theta, angle)
        angle = angle * 0.5
        v_cos = torch.cos(angle)
        v_sin = torch.sin(angle)
        quat = torch.cat([v_sin * normalized, v_cos - 1.0], dim=1)
        return quat

    def _quat2mat(self, quat: torch.Tensor) -> torch.Tensor:
        '''
            Converts a quaternion to a rotation matrix
        :param quat:
        :return:
        '''
        norm_quat = quat
        norm_quat = norm_quat / norm_quat.norm(p=2, dim=1, keepdim=True)
        w, x, y, z = norm_quat[:, 0], norm_quat[:, 1], norm_quat[:, 2], norm_quat[:, 3]
        B = quat.size(0)
        w2, x2, y2, z2 = w.pow(2), x.pow(2), y.pow(2), z.pow(2)
        wx, wy, wz = w * x, w * y, w * z
        xy, xz, yz = x * y, x * z, y * z
        rotMat = torch.stack([
            w2 + x2 - y2 - z2, 2 * xy - 2 * wz, 2 * wy + 2 * xz,
            2 * wz + 2 * xy, w2 - x2 + y2 - z2, 2 * yz - 2 * wx,
            2 * xz - 2 * wy, 2 * wx + 2 * yz, w2 - x2 - y2 + z2
        ], dim=1).view(B, 3, 3)
        return rotMat

    def _rodrigues(self, theta: torch.Tensor) -> torch.Tensor:
        '''
            Computes the rodrigues representation given joint angles

        :param theta: batch_size x number of joints x 3
        :return: batch_size x number of joints x 3 x 4
        '''
        l1norm = torch.norm(theta + 1e-8, p = 2, dim = 1)
        angle = torch.unsqueeze(l1norm, -1)
        normalized = torch.div(theta, angle)
        angle = angle * 0.5
        v_cos = torch.cos(angle)
        v_sin = torch.sin(angle)
        quat = torch.cat([v_cos, v_sin * normalized], dim = 1)
        return self._quat2mat(quat)

    def _with_zeros(self, input: torch.Tensor) -> torch.Tensor:
        '''
        Appends a row of [0,0,0,1] to a batch size x 3 x 4 Tensor

        :param input: A tensor of dimensions batch size x 3 x 4
        :return: A tensor batch size x 4 x 4 (appended with 0,0,0,1)
        '''
        b = input.shape[0]
        row_append = torch.Tensor(([0.0, 0.0, 0.0, 1.0])).to(input)
        row_append.requires_grad = False
        padded_tensor = torch.cat([
            input, row_append.view(1, 1, 4).repeat(b, 1, 1)
        ], dim=1)
        return padded_tensor

    def forward(self, 
        pose:           torch.Tensor,
        betas:          torch.Tensor,
        rotation:       torch.Tensor=None,
        translation:    torch.Tensor=None,
        left_hand:      torch.Tensor=None,
        right_hand:     torch.Tensor=None,
    ):
        '''
            STAR forward pass given pose, betas (shape) and trans
            return the model vertices and transformed joints
        :param pose: pose  parameters - A batch size x 72 tensor (3 numbers for each joint)
        :param beta: beta  parameters - A batch size x number of betas
        :param beta: trans parameters - A batch size x 3
        :return:
                 v         : batch size x 6890 x 3
                             The STAR model vertices
                 v.v_vposed: batch size x 6890 x 3 model
                             STAR vertices in T-pose after adding the shape
                             blend shapes and pose blend shapes
                 v.v_shaped: batch size x 6890 x 3
                             STAR vertices in T-pose after adding the shape
                             blend shapes and pose blend shapes
                 v.J_transformed:batch size x 24 x 3
                                Posed model joints.
                 v.f: A numpy array of the model face.
        '''
        if rotation is not None:
            pose = torch.cat([rotation, pose], dim=1)
        else:
            rotation = torch.zeros(1, 3, device=pose.device)
            pose = torch.cat([rotation, pose], dim=1)
        if left_hand is not None and right_hand is not None:
            pose = torch.cat([pose, left_hand, right_hand], dim=1)
        elif pose.shape[-1] < 72:
            left_hand = torch.zeros_like(rotation)
            right_hand = torch.zeros_like(rotation)
            pose = torch.cat([pose, left_hand, right_hand], dim=1)
        if translation is None:
            translation = torch.zeros_like(rotation)
        b = pose.shape[0]
        v_template = self.v_template[np.newaxis, :]
        shapedirs = self.shapedirs.view(-1, self.num_betas)[np.newaxis, :].expand(b, -1, -1)
        beta = betas[:, :, np.newaxis]
        v_shaped = torch.matmul(
            shapedirs, beta
        ).view(-1, 6890, 3) + v_template
        J = torch.einsum('bik,ji->bjk', [v_shaped, self.J_regressor])

        pose_quat = self._quat_feat(pose.view(-1, 3)).view(b, -1)
        pose_feat = torch.cat((pose_quat[:, 4:], beta[:, 1]), 1)

        R = self._rodrigues(pose.view(-1, 3)).view(b, 24, 3, 3)
        R = R.view(b, 24, 3, 3)#NOTE: get joint count

        posedirs = self.posedirs[np.newaxis, :].expand(b, -1, -1)
        v_posed = v_shaped + torch.matmul(
            posedirs, pose_feat[:, :, np.newaxis]
        ).view(-1, 6890, 3)#NOTE: get face count
        
        J_ = J.clone()
        J_[:, 1:, :] = J[:, 1:, :] - J[:, self.parent, :]

        G_ = torch.cat([R, J_[:, :, :, np.newaxis]], dim=-1)
        pad_row = torch.Tensor([0.0, 0.0, 0.0, 1.0]).to(
            self.faces.device
        ).view(1, 1, 1, 4).expand(b, 24, -1, -1)
        G_ = torch.cat([G_, pad_row], dim=2)
        G = [G_[:, 0].clone()]        
        for i in range(1, 24):
            G.append(torch.matmul(G[self.parent[i - 1]], G_[:, i, :, :]))
        G = torch.stack(G, dim=1)

        rest = torch.cat(
            [J, torch.zeros_like(pad_row[..., 0])], dim=2
        ).view(b, 24, 4, 1)
        zeros = torch.zeros(b, 24, 4, 3).to(rest.device)
        rest = torch.cat([zeros, rest], dim=-1)
        rest = torch.matmul(G, rest)
        G = G - rest
        T = torch.matmul(
            self.weights, G.permute(1, 0, 2, 3).contiguous().view(24, -1)
        ).view(6890, b, 4, 4).transpose(0, 1)
        rest_shape_h = torch.cat(
            [v_posed, torch.ones_like(v_posed)[:, :, [0]]], dim=-1
        )
        v = torch.matmul(T, rest_shape_h[:, :, :, np.newaxis])[:, :, :3, 0]
        v = v + translation[:, np.newaxis, :]
            
        root_transform = self._with_zeros(
            torch.cat((R[:, 0], J[:, 0][:, :, np.newaxis]), 2)
        )
        results =  [root_transform]
        for i in range(0, self.parent.shape[0]):
            transform_i = self._with_zeros(
                torch.cat((
                    R[:, i + 1], J[:, i + 1][:, :, np.newaxis] - J[:, self.parent[i]][:, :, np.newaxis]), dim=2
                ))
            curr_res = torch.matmul(results[self.parent[i]], transform_i)
            results.append(curr_res)
        results = torch.stack(results, dim=1)
        posed_joints = results[:, :, :3, 3]
        J_transformed = posed_joints + translation[:, np.newaxis, :]
        
        res = { }
        res['vertices'] = v
        res['faces'] = self.faces.expand(b, -1, -1) #NOTE: or self.faces and ignore the np.ndarray self.f
        res['posed'] = v_posed
        res['shape'] = v_shaped
        res['joints'] = torch.cat([
            self.joint_mapper(J_transformed, v),
            _extract_face(v),
        ], dim=-2) if self.append_face else self.joint_mapper(J_transformed, v)
        res['betas'] = betas
        return res
    
class SMPLX2STAR(torch.nn.Module):
    def __init__(self,
        transfer_weights:       str,
    ) -> None:
        super(SMPLX2STAR, self).__init__()
        xfer_mat = torch.from_numpy(
            np.load(
                transfer_weights, allow_pickle=True, encoding='latin1'
            )[()]['mtx'].toarray()
        )[:, :10475]
        self.register_buffer('xfer_mat', xfer_mat[np.newaxis, ...].float())

    def forward(self,
        vertices:       torch.Tensor,
    ) -> torch.Tensor:
        return torch.einsum('bsx,bxv->bsv', self.xfer_mat, vertices)

class Offset(torch.nn.Module):

    __VALUES__ = [[[0.0, 0.15, 0.025]]]

    def __init__(self) -> None:
        super(Offset, self).__init__()
        self.register_buffer('offset', torch.Tensor(Offset.__VALUES__))

    def forward(self,
        smplx_points:       torch.Tensor=None,
        star_points:        torch.Tensor=None,
    ) -> torch.Tensor:
        if smplx_points is not None:
            return smplx_points + (self.offset if len(smplx_points.shape) > 2 else self.offset.squeeze())
        if star_points is not None:
            return star_points - (self.offset if len(star_points.shape) > 2 else self.offset.squeeze())
        
class ToOpenPose(torch.nn.Module):
    def __init__(self,
        neck_indices:   typing.List[int]=[12, 13, 14],
    ) -> None:
        super(ToOpenPose, self).__init__()

    def forward(self, 
        joints:     torch.Tensor,
        vertices:   torch.Tensor,
    ) -> torch.Tensor:
        return torch.cat([
            _star_to_openpose(joints=joints, vertices=vertices),
            _extract_face(vertices),
        ], dim=-2)     