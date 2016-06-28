#!/usr/bin/python
""" Classes and functions for fitting ivim model """
from __future__ import division, print_function, absolute_import
import warnings
import functools
import numpy as np
from dipy.core.optimize import Optimizer

from scipy.optimize import leastsq

from dipy.core.gradients import gradient_table
from dipy.reconst.base import ReconstModel
from dipy.reconst.dti import _min_positive_signal, apparent_diffusion_coef
from dipy.reconst.dti import TensorModel, mean_diffusivity
from .vec_val_sum import vec_val_vect
from dipy.core.sphere import Sphere


def ivim_function(params, bvals):
    """The ivim function
    Parameters
    ----------
    params : array
             The IVIM parameters [S0, f, D_star, D]

    bvals  : array
             bvalues
    """
    S0, f, D_star, D = params
    S = S0 * (f * np.exp(-bvals * D_star) + (1 - f) * np.exp(-bvals * D))
    return S


def _ivim_error(params, bvals, signal):
    """Error function to be used in fitting the model
    """
    return (signal - ivim_function(params, bvals))


class IvimModel(ReconstModel):
    """Ivim model
    """

    def __init__(self, gtab, split_b=200.0, min_signal=None):
        """
        An IVIM model

        Parameters
        ----------
        gtab : GradientTable class instance
        split_b : split the data at this b-value for a two stage fit

        References
        ----------
        .. [1] Le Bihan, Denis, et al. "Separation of diffusion
                   and perfusion in intravoxel incoherent motion MR
                   imaging." Radiology 168.2 (1988): 497-505.
        .. [2] Federau, Christian, et al. "Quantitative measurement
                   of brain perfusion with intravoxel incoherent motion
                   MR imaging." Radiology 265.3 (2012): 874-881.
        """
        ReconstModel.__init__(self, gtab)
        self.split_b = split_b
        self.min_signal = min_signal
        if self.min_signal is not None and self.min_signal <= 0:
            e_s = "The `min_signal` key-word argument needs to be strictly"
            e_s += " positive."
            raise ValueError(e_s)

    def fit(self, data, mask=None, x0=None, fit_method="one_stage", routine="minimize",
            jac=None, bounds=((0, None), (0, 1.), (0, 1.), (0, 1.)), tol=1e-25, algorithm='L-BFGS-B',
            gtol=1e-25, ftol=1e-25, eps=1e-15, maxiter=1000):
        """ Fit method of the Ivim model class

        Parameters
        ----------
        data : array
            The measured signal from one voxel.
        mask : array
            A boolean array used to mark the coordinates in the data that
            should be analyzed that has the shape data.shape[:-1]
        x0 : array
            Initial guesses for the parameters S0, f, D_star and D
            Default x0 = [1.0, 0.10, 0.001, 0.0009]
            These guess parameters are taken from the Federau paper
            Dimension can either be 1 or (N, 4) where N is the number of
            voxels in the data.
        fit_method: str
            Use one-stage fitting or two-stage fitting.
        jac : Boolean
            Use the Jacobian
        bounds : tuple
            Bounds for the various parameters
        tol : float
            tolerance for convergence
        method : str
            Fitting algorithm to use from scipy.optimize

        Returns
        -------
        IvimFit object
        """
        if mask is None:
            # Flatten it to 2D either way:
            data_in_mask = np.reshape(data, (-1, data.shape[-1]))
        else:
            # Check for valid shape of the mask
            if mask.shape != data.shape[:-1]:
                raise ValueError("Mask is not the same shape as data.")
            mask = np.array(mask, dtype=bool, copy=False)
            data_in_mask = np.reshape(data[mask], (-1, data.shape[-1]))

        if self.min_signal is None:
            min_signal = _min_positive_signal(data)
        else:
            min_signal = self.min_signal

        # Generate guess_parameters for all voxels
        if x0 is None:
            x0 = np.ones(
                (data.shape[:-1] + (4,))) * [1.0, 0.10, 0.001, 0.0009]
        else:
            if x0.shape != (data.shape[0], 4):
                raise ValueError(
                    "Guess params should be of shape (num_voxels,4)")
        data_in_mask = np.maximum(data_in_mask, min_signal)
        x0 = np.reshape(x0[mask], (-1, x0.shape[-1]))

        if fit_method == "one_stage":
            params_in_mask = one_stage(data_in_mask, self.gtab, x0,
                                       jac, bounds, tol,
                                       routine,
                                       algorithm, gtol, ftol, eps, maxiter)
        elif fit_method == "two_stage":
            params_in_mask = two_stage(data_in_mask, self.gtab, x0,
                                       self.split_b, jac, bounds, tol,
                                       routine,
                                       algorithm, gtol, ftol, eps, maxiter)
        else:
            raise ValueError("""Fit method must be either
                             'one_stage' or 'two_stage'""")

        if mask is None:
            out_shape = data.shape[:-1] + (-1, )
            ivim_params = params_in_mask.reshape(out_shape)
        else:
            ivim_params = np.zeros(data.shape[:-1] + (4,))
            ivim_params[mask, :] = params_in_mask

        return IvimFit(self, ivim_params)

    def predict(self, ivim_params):
        """
        Predict a signal for this IvimModel class instance given parameters.

        Parameters
        ----------
        ivim_params : ndarray
            The last dimension should have 4 parameters: S0, f, D_star and D
        """

        return ivim_function(ivim_params, self.gtab.bvals)


class IvimFit(object):

    def __init__(self, model, model_params):
        """ Initialize a IvimFit class instance.
            Parameters
            ----------
            The model parameters are S0, f, D_star, D
        """
        self.model = model
        self.model_params = model_params

    def __getitem__(self, index):
        model_params = self.model_params
        N = model_params.ndim
        if type(index) is not tuple:
            index = (index,)
        elif len(index) >= model_params.ndim:
            raise IndexError("IndexError: invalid index")
        index = index + (slice(None),) * (N - len(index))
        return type(self)(self.model, model_params[index])

    @property
    def shape(self):
        return self.model_params.shape[:-1]

    def predict(self, gtab, step=None, S0=1.0):
        r"""
        Given a model fit, predict the signal.

        Parameters
        ----------
        gtab : a GradientTable class instance
            This encodes the directions for which a prediction is made

        Notes
        -----
        The predicted signal is given by:

        .. math ::



        Where:
        .. math ::

        """
        return ivim_function(self.model_params, gtab.bvals)


def one_stage(data, gtab, x0, jac, bounds, tol, routine, algorithm,
              gtol, ftol, eps, maxiter):
    """
    Fit the ivim params using minimize

    Parameters
    ----------
    data : array ([X, Y, Z, ...], g)
        Data or response variables holding the data. Note that the last
        dimension should contain the data. It makes no copies of data.

    jac : bool
        Use the Jacobian? Default: False

    Returns
    -------
    ivim_params: the S0, f, D_star, D value for each voxel.

    """
    flat_data = data.reshape((-1, data.shape[-1]))
    flat_x0 = x0.reshape(-1, x0.shape[-1])
    # Flatten for the iteration over voxels:
    bvals = gtab.bvals
    ivim_params = np.empty((flat_data.shape[0], 4))
    flat_x0[..., 0] = flat_data[..., 0]

    if routine == 'minimize':
        result = _minimize(flat_data, bvals, flat_x0, ivim_params,
                           bounds, tol, jac, algorithm, gtol, ftol, eps, maxiter)
    elif routine == "leastsq":
        result = _leastsq(flat_data, bvals, flat_x0, ivim_params)
    ivim_params.shape = data.shape[:-1] + (4,)
    return ivim_params


def _minimize(flat_data, bvals, flat_x0, ivim_params,
              bounds, tol, jac, algorithm, gtol, ftol, eps, maxiter):
    """Use minimize for finding ivim_params"""
    sum_sq = lambda params, bvals, signal: np.sum(
        _ivim_error(params, bvals, signal)**2)

    result = []
    if jac == True:
        jacobian = _ivim_jacobian_func
    else:
        jacobian = None
    for vox in range(flat_data.shape[0]):
        res = Optimizer(sum_sq,
                        flat_x0[vox],
                        args=(bvals, flat_data[vox]), bounds=bounds,
                        tol=tol, method=algorithm, jac=jacobian,
                        options={'gtol': gtol, 'ftol': ftol, 'eps': eps, 'maxiter': maxiter})
        ivim_params[vox, :4] = res.xopt
        result += [res]
    return result


def _leastsq(flat_data, bvals, flat_x0, ivim_params):
    """Use minimize for finding ivim_params"""
    result = []
    for vox in range(flat_data.shape[0]):
        res = leastsq(_ivim_error,
                      flat_x0[vox],
                      args=(bvals, flat_data[vox]))
        ivim_params[vox, :4] = res[0]
        result += [res]
    return result


def two_stage(data, gtab, x0,
              split_b, jac, bounds, tol,
              routine, algorithm, gtol, ftol, eps, maxiter):
    """
    Fit the ivim params using a two stage fit

    Parameters
    ----------
    data : array ([X, Y, Z, ...], g)
        Data or response variables holding the data. Note that the last
        dimension should contain the data. It makes no copies of data.

    jac : bool
        Use the Jacobian? Default: False

    Returns
    -------
    ivim_params: the S0, f, D_star, D value for each voxel.

    """
    flat_data = data.reshape((-1, data.shape[-1]))
    flat_x0 = x0.reshape(-1, x0.shape[-1])
    # Flatten for the iteration over voxels:
    bvals = gtab.bvals
    ivim_params = np.empty((flat_data.shape[0], 4))

    flat_x0[..., 0] = flat_data[..., 0]
    D_guess = get_D_guess(flat_data, gtab, split_b)
    S0_hat = get_S0_guess(flat_data, gtab, split_b)
    f_guess = 1 - S0_hat / flat_data[..., 0]

    flat_x0[..., 1] = f_guess
    flat_x0[..., 3] = D_guess

    if routine == 'minimize':
        result = _minimize(flat_data, bvals, flat_x0, ivim_params,
                           bounds, tol, jac, algorithm, gtol, ftol, eps, maxiter)
    elif routine == "leastsq":
        result = _leastsq(flat_data, bvals, flat_x0, ivim_params)
    ivim_params.shape = data.shape[:-1] + (4,)
    return ivim_params


def get_D_guess(data, gtab, split_b):
    bvals_ge_split = gtab.bvals[gtab.bvals > split_b]
    bvecs_ge_split = gtab.bvecs[gtab.bvals > split_b]
    gtab_ge_split = gradient_table(bvals_ge_split, bvecs_ge_split.T)
    tensor_model = TensorModel(gtab_ge_split)
    tenfit = tensor_model.fit(data[..., gtab.bvals > split_b])
    D_guess = mean_diffusivity(tenfit.evals)
    return D_guess


def get_S0_guess(data, gtab, split_b):
    bvals_ge_split = gtab.bvals[gtab.bvals > split_b]
    bvecs_ge_split = gtab.bvecs[gtab.bvals > split_b]
    gtab_ge_split = gradient_table(bvals_ge_split, bvecs_ge_split.T)
    tensor_model = TensorModel(gtab_ge_split)
    tenfit = tensor_model.fit(data[..., gtab.bvals > split_b])

    dti_params = tenfit.model_params
    evecs = dti_params[..., 3:12].reshape((dti_params.shape[:-1] + (3, 3)))
    evals = dti_params[..., :3]
    qform = vec_val_vect(evecs, evals)

    sphere = Sphere(xyz=gtab.bvecs[~gtab.b0s_mask])
    ADC = apparent_diffusion_coef(qform, sphere)

    S0_hat = np.mean(data[..., ~gtab.b0s_mask] /
                     np.exp(-gtab.bvals[~gtab.b0s_mask] * ADC),
                     -1)
    return S0_hat


def _ivim_jacobian_func(params, bvals, signal):
    """The Jacobian is the first derivative of the error function.

    Notes
    -----
    The worked out Jacobian can be found here :
    http://mathb.in/64905?key=774f1d2b7c71358b4cf6dd0e6e4f5de3a5b5fbe3

    References
    ----------

    """
    S0, f, D_star, D = params

    derv_S0 = f * np.exp(-bvals * D_star) + (1 - D) * np.exp(-bvals * D)
    derv_f = S0 * (np.exp(-bvals * D_star) - np.exp(-bvals * D))
    derv_D_star = S0 * (-bvals * f * np.exp(-bvals * D_star))
    derv_D = S0 * (-bvals * (1 - f) * np.exp(-bvals * D))

    jacobian = np.zeros((len(params)))

    jacobian[0] = np.sum(2 * _ivim_error(params, bvals, signal) * derv_S0)
    jacobian[1] = np.sum(2 * _ivim_error(params, bvals, signal) * derv_f)
    jacobian[2] = np.sum(2 * _ivim_error(params, bvals, signal) * derv_D_star)
    jacobian[3] = np.sum(2 * _ivim_error(params, bvals, signal) * derv_D)

    return jacobian
