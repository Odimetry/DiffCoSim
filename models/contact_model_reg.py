"""
Non-commercial Use License

Copyright (c) 2021 Siemens Technology

This software, along with associated documentation files (the "Software"), is 
provided for the sole purpose of providing Proof of Concept. Any commercial 
uses of the Software including, but not limited to, the rights to sublicense, 
and/or sell copies of the Software are prohibited and are subject to a 
separate licensing agreement with Siemens. This software may be proprietary 
to Siemens and may be covered by patent and copyright laws. Processes 
controlled by the Software are patent pending.

The above copyright notice and this permission notice shall remain attached 
to the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR 
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE 
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, 
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import fsspec, os
import torch
import torch.nn as nn
import torch.nn.functional as F
import cvxpy as cp
from cvxpylayers.torch import CvxpyLayer
from diffcp.cone_program import SolverError
# from symeig import symeig
from .contact_model import ContactModel

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ContactModelReg(ContactModel):
    def __init__(
        self, dt, n_o, n_p, d, 
        check_collision, cld_2did_to_1did, DPhi,  
        ls, bdry_lin_coef, delta=None, 
        get_limit_e_for_Jac=None,
        get_3d_contact_point_c_tilde=None,
        limit_idx_to_o_idx=None,
        save_dir=os.path.join(PARENT_DIR, "tensors"),
        reg=0.01,
    ):
        super().__init__(dt, n_o, n_p, d, 
        check_collision, cld_2did_to_1did, DPhi,  
        ls, bdry_lin_coef, delta, 
        get_limit_e_for_Jac,
        get_3d_contact_point_c_tilde,
        limit_idx_to_o_idx,
        save_dir)
        self.reg = reg

    def get_dv_w_J_e(self, bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor, DPhi):
        n_cld, d, n_o, n_p, _ = Jac.shape
        n = n_o*n_p
        C = DPhi.shape[-1]
        # calculate A_decom
        J_e_T = DPhi[:,0,:,:,0,:].reshape(n*d, C)
        J_e = J_e_T.t() # 
        Minv_J_e_T = (Minv @ J_e_T.reshape(n, d*C)).reshape(n*d, C)
        J_e_Minv_J_e_T = J_e @ Minv_J_e_T # (C, C)
        M_e = torch.inverse(J_e_Minv_J_e_T) # (C, C) inertia in equality contact space
        factor = torch.eye(n*d).type_as(v) - J_e_T @ M_e @ Minv_J_e_T.t()
        M_hat_inv = (Minv @ factor.reshape(n, d*n*d)).reshape(n*d, n*d)
        A = Jac.reshape(n_cld*d, n*d) @ M_hat_inv @ Jac.reshape(n_cld*d, n*d).t()
        
        if torch.is_tensor(self.reg):
            reg = F.softplus(self.reg)
        else:
            reg = self.reg
        A_decom = torch.linalg.cholesky(A+torch.eye(A.shape[0]).type_as(A)*reg).mH
        # make sure v_star is "valid", avoid unbounded cvx problem
        v_star_c = self.get_v_star_c_w_J_e(n_cld, n, d, v_star, Jac.reshape(n_cld*d, n*d), J_e)

        # compression phase impulse
        try:
            impulse = self.solve_compression_impulse(
                A_decom, Jac_v, mu, n_cld, d
            )
        except SolverError as e:
            try:
                impulse = self.solve_compression_impulse_w_penalty(
                    A_decom, Jac_v, mu, n_cld, d
                )
            except SolverError as e:
                # self.save(e, "comp", bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor, DPhi)
                impulse = torch.zeros(n_cld*d, 1).type_as(v)
        # velocity after compression phase (before retitution phase)
        M_hat_inv_Jac_T = M_hat_inv @ Jac.reshape(n_cld*d, n*d).t() # (n*d, n_cld*d)
        dv_comp = (M_hat_inv_Jac_T @ impulse).reshape(n, d)
        v_prev_r = v[bs_idx] + dv_comp
        # restitution phase impulse
        Jac_v_prev_r = Jac.reshape(n_cld*d, n*d) @ v_prev_r.reshape(n*d, 1) # n_cld*d, 1
        target_impulse = (cor.reshape(-1) * impulse.reshape(n_cld, d)[:, 0]).reshape(n_cld, 1)
        try:
            impulse_star_r = self.solve_restitution_impulse(
                A_decom, Jac_v_prev_r, v_star_c, mu, n_cld, d, target_impulse=target_impulse
            )
        except SolverError as e:
            try:
                impulse_star_r = self.solve_restitution_impulse_w_penalty(
                    A_decom, Jac_v_prev_r, v_star_c, mu, n_cld, d, target_impulse=target_impulse
                )
            except SolverError as e:
                # self.save(e, "rest", bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor, DPhi)
                impulse_star_r = torch.zeros(n_cld*d, 1).type_as(v)
        # velocity after restitution phase, compensate penetration
        dv_rest = (M_hat_inv_Jac_T @ impulse_star_r).reshape(n, d)
        dv = torch.zeros_like(v)
        dv[bs_idx] = dv_comp + dv_rest
        return dv

    def get_dv_wo_J_e(self, bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor):
        n_cld, d, n_o, n_p, _ = Jac.shape
        n = n_o * n_p
        # calculate A_decom
        Minv_Jac_T = (Minv @ Jac.reshape(n_cld*d, n*d).t().reshape(n, d*n_cld*d)).reshape(n*d, n_cld*d)
        A = Jac.reshape(n_cld*d, n*d) @ Minv_Jac_T

        if torch.is_tensor(self.reg):
            reg = F.softplus(self.reg)
        else:
            reg = self.reg
        A_decom = torch.linalg.cholesky(A+torch.eye(A.shape[0]).type_as(A)*reg).mH
        # make sure v_star is "valid", avoid unbounded cvx problem
        with torch.no_grad():
            if n_cld > n: # Jac is a tall matrix
                # v_star_c = J (J_T J)^-1 J_T v_star
                try:
                    v_star_euc = Jac.reshape(n_cld*d, n*d).t() @ v_star.reshape(n_cld*d, 1) # (n*d, 1)
                    v_star_euc = torch.linalg.lstsq(Jac.reshape(n_cld*d, n*d).t() @ Jac.reshape(n_cld*d, n*d), v_star_euc)[0]
                    v_star_c = Jac.reshape(n_cld*d, n*d) @ v_star_euc
                except RuntimeError as e:
                    # https://math.stackexchange.com/questions/748500/how-to-find-linearly-independent-columns-in-a-matrix
                    Q, R = torch.qr(Jac.reshape(n_cld*d, n*d), some=True)
                    # Q: (n_cld*d, n*d), R: (n*d, n*d)
                    idx_list = [0] ; ptr = 0
                    while ptr < n-1:
                        for ptr_r in range(ptr+1,n*d):
                            if R[len(idx_list), ptr_r] != 0:
                                idx_list.append(ptr_r)
                                ptr = ptr_r
                                break
                    Jac_full_rank = Q @ R[:, idx_list]
                    v_star_euc = Jac_full_rank.t() @ v_star.reshape(n_cld*d, 1)
                    v_star_euc = torch.linalg.solve(Jac_full_rank.t() @ Jac_full_rank, v_star_euc)[0]
                    v_star_c = Jac_full_rank @ v_star_euc
                    # print("performed QR decomposition")
            else:
                v_star_c = v_star
        # compression phase impulse
        try:
            impulse = self.solve_compression_impulse(
                A_decom, Jac_v, mu, n_cld, d
            )
        except SolverError as e:
            try:
                impulse = self.solve_compression_impulse_w_penalty(
                    A_decom, Jac_v, mu, n_cld, d
                )
            except SolverError as e:
                # self.save(e, "comp", bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor)
                impulse = torch.zeros(n_cld*d, 1).type_as(v)
        # velocity after compression phase (before retitution phase)
        dv_comp = (Minv_Jac_T @ impulse).reshape(n, d)
        v_prev_r = v[bs_idx] + dv_comp
        # restitution phase impulse
        Jac_v_prev_r = Jac.reshape(n_cld*d, n*d) @ v_prev_r.reshape(n*d, 1) # n_cld*d, 1
        target_impulse = (cor.reshape(-1) * impulse.reshape(n_cld, d)[:, 0]).reshape(n_cld, 1)
        try:
            impulse_star_r = self.solve_restitution_impulse(
                A_decom, Jac_v_prev_r, v_star_c, mu, n_cld, d, target_impulse=target_impulse
            )
        except SolverError as e:
            try:
                impulse_star_r = self.solve_restitution_impulse_w_penalty(
                    A_decom, Jac_v_prev_r, v_star_c, mu, n_cld, d, target_impulse=target_impulse
                )
            except:
                # self.save(e, "rest", bs_idx, v, Minv, Jac, Jac_v, v_star, mu, cor)
                impulse_star_r = torch.zeros(n_cld*d, 1).type_as(v)
        # velocity after restitution phase
        dv_rest = (Minv_Jac_T @ impulse_star_r).reshape(n, d)
        dv = torch.zeros_like(v)
        dv[bs_idx] = dv_comp + dv_rest
        return dv

