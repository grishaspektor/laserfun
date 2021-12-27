import numpy as np
import matplotlib.pyplot as plt
from numpy import linspace, pi, log10, exp
from scipy.special import factorial
from scipy.integrate import complex_ode
from scipy import constants
import scipy.ndimage
import time
import nlse

def nlse(pulse, fiber, nsaves=200, atol=1e-4, rtol=1e-4, reload_fiber=False,
         raman=False, shock=True, integrator='lsoda', print_status=True):
    """
    This function propagates an optical input field (often a laser pulse)
    through a nonlinear material using the generalized nonlinear
    Schrodinger equation, which takes into account dispersion and
    nonlinearity. It is a "one dimensional" calculation, in that it doesn't
    capture things like self focusing and other geometric effects. It's most
    appropriate for analyzing light propagating through optical fibers.

    This code is based on the Matlab code found at www.scgbook.info,
    which is based on Eqs. (3.13), (3.16) and (3.17) of the book
    "Supercontinuum Generation in Optical Fibers" Edited by J. M. Dudley and
    J. R. Taylor (Cambridge 2010).
    The original Matlab code was written by J.C. Travers, M.H. Frosz and J.M.
    Dudley (2009). They ask that you cite this chapter in any publications using
    their code.

    Parameters
    ----------
    pulse : pulse object
        This is the input pulse.
    fiber : fiber object
        This defines the media ("fiber") through which the pulse propagates.
    nsaves : int
        The number of equidistant grid points along the fiber to return
        the field. Note that the integrator usually takes finer steps than
        this, the nsaves parameters simply determines what is returned by this
        function.
    atol : float
        Absolute tolerance for the integrator. Smaller values produce more
        accurate results but require longer integration times. 1e-4 works well.
    rtol : float
        Relative tolerance for the integrator. 1e-4 work well. 
    reload_fiber : boolean
        This determines if the fiber information is reloaded at each step. This
        should be set to True if the fiber properties (gamma, dispersion) vary
        as a function of length.
    raman : boolean
        Determines if the Raman effect will be included. Default is False.
    shock : boolean
        Determines if the self-steepening (shock) term will be taken into
        account. This is especially important for situations where the
        slowly varying envelope approximation starts to break down, which 
        can occur for large bandwidths (short pulses).
    integrator : string
        Selects the integrator that will be passes to scipy.integrate.ode.
        options are 'lsoda' (default), 'vode', 'dopri5', 'dopri853'.
        'lsoda' is a good option, and seemed fastest in early tests.
        I think 'dopri5' and 'dopri853' are simpler Runge-Kutta methods,
        and they seem to take longer for the same result.
        'vode' didn't seem to produce good results with "method='adams'", but
        things werereasonable with "method='bdf'"
        For more information, see:
        docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.ode.html
    print_status : boolean
         This determines if the propagation status will be printed. Default
         is True.

    Returns
    -------
    z : 1D numpy array of length nsaves
        array of the z-coordinate along the fiber where the results are returned
    AT : 2D numpy array, with dimensions nsaves x n
        The complex amplitude of the time domain field at every step.
    AW : 2D numpy array, with dimensions nsaves x n
        The complex amplitide of the frequency domain field at every step.
    f : 1D numpy array of length n
        The frequency grid (not angular freq).
    """


    from scipy.fftpack import fft, ifft, fftshift

    # get the pulse info from the pulse object:
    t = pulse.t_ps  #  time array in picoseconds
    at = pulse.at   #  amplitude for those times in sqrt(W)
    w0 = pulse.centerfrequency_THz * 2 * np.pi  # center freq (angular!)

    n = t.size        # number of time/frequency points
    dt = t[1] - t[0]  # time step
    v = 2 * pi * linspace(-0.5/dt, 0.5/dt, n)  # *angular* frequency grid

    flength = fiber.length  # get length of fiber
    
    def load_fiber(fiber, z=0):
        # gets the fiber info from the fiber object
        gamma = fiber.get_gamma(z)  # gamma should be in 1/(W m), not 1/(W km)
        b = fiber.get_B(pulse, z)
        
        loss = np.log(10**(fiber.get_alpha(z)*0.1))  # convert from dB/m
        
        lin_operator = 1j*b - loss*0.5  # linear operator

        if w0>0 and shock:          # if w0>0 then include shock
            gamma = gamma/w0
            w = v + w0              # for shock w is true freq
        else:
            w = 1 + v*0             # set w to 1 when no shock

        # some fft shifts to things line up later:
        lin_operator = fftshift(lin_operator)
        w = fftshift(w)
        return lin_operator, w, gamma

    lin_operator, w, gamma = load_fiber(fiber) # load fiber info

    # Raman response:
    if raman == 'dudley' or raman == True:
        fr  =0.18; t1 = 0.0122; t2 = 0.032
        rt = (t1**2+t2**2)/t1/t2**2*np.exp(-t/t2)*np.sin(t/t1)
        rt[t < 0] = 0                # heaviside step function
        rw = n * ifft(fftshift(rt))  # frequency domain Raman
    elif raman == False:
        fr = 0
    else:
        raise ValueError('Raman method not supported')

    # define function to return the RHS of Eq. (3.13):
    def rhs(z, aw):
        nonlocal lin_operator, w, gamma

        if reload_fiber:
            lin_operator, w, gamma = load_fiber(fiber, z)

        at = fft(aw * exp(lin_operator*z))    # time domain field
        it = np.abs(at)**2                    # time domain intensity

        if np.isclose(fr, 0):  # no Raman case
            m = ifft(at*it)                    # response function
        else:
            rs = dt * fr * fft(ifft(it) * rw)     # Raman convolution
            m = ifft(at*((1-fr)*it + rs))         # response function

        r = 1j * gamma * w * m * exp(-lin_operator*z)  # full RHS of Eq. (3.13)
        return r

    z = linspace(0, flength, nsaves)    # select output z points
    aw = ifft(at.astype('complex128'))  # ensure integrator knows it's complex

    # set up the integrator: 
    r = complex_ode(rhs).set_integrator(integrator, atol=atol, rtol=rtol)
    r.set_initial_value(aw, z[0])  

    # intialize array for results:
    AW = np.zeros((z.size, aw.size), dtype='complex128')
    AW[0] = aw        # store initial pulse as first row

    start_time = time.time()  # start the timer

    for count, zi in enumerate(z[1:]):

        if print_status:
            print('% 6.1f%% complete - %.3e m - %.1f seconds' % ((zi/z[-1])*100,
                  zi, time.time()-start_time))
        if not r.successful():
            raise Exception('Integrator failed! Check the input parameters.')

        AW[count+1] = r.integrate(zi)

    # process the output:
    AT = np.zeros_like(AW)
    for i in range(len(z)):
        AW[i] = AW[i] * exp(lin_operator.transpose()*z[i])  # change variables
        AT[i, :] = fft(AW[i])            # time domain output
        AW[i, :] = fftshift(AW[i])

        # Below is the original dudley scaling factor that I think gives units
        # of sqrt(J/Hz) for the AW array. Removing this gives units that agree
        # with PyNLO, that seem to be sqrt(J*Hz) = sqrt(Watts) -DH 2021-12-15
        # AW[i, :] = AW[i, :] * dt * n
    
    pulse_out = pulse.create_cloned_pulse()
    pulse_out.at = AT[-1]
    
    res = PulseData(z, AT, AW, pulse, pulse_out, fiber)

    return res

class PulseData:
    """This class holds the data from a pulse propagation. Mainly, it contains 
    the amplitude of the pulse in the time and frequency domain at each step.
    
    Parameters
    ----------
    z : array of length n
        The z-values corresponding the the propagation.
    AT : 2D array
        The complex amplitude of the electric field in the time domain at each 
        position in z.
    AW : 2D array
        The complex amplitude of the electric field in the freq. domain at each 
        position in z.
    pulse_in : pulse object
        The input pulse object.
    pulse_out : pulse object
        The output pulse object.
    fiber : fiber object
        The fiber object that the pulse was propagated through.
        
    Attributes
    ----------
    z : array of length n
        The z-values corresponding the the propagation.
    f : array of length m
        The values of the frequency grid in THz.
    t : array of length m
        The values of time grid in ps
    AT : 2D array
        The complex amplitude of the electric field in the time domain at each 
        position in z.
    AW : 2D array
        The complex amplitude of the electric field in the freq. domain at each 
        position in z.
    pulse_in : pulse object
        The input pulse object.
    pulse_out : pulse object
        The output pulse object.
    fiber : fiber object
        The fiber object that the pulse was propagated through.
    """
    def __init__(self, z, AT, AW, pulse_in, pulse_out, fiber):
        self.z = z
        self.AW = AW
        self.AT = AT
        self.pulse_in = pulse_in
        self.pulse_out = pulse_out
        self.fiber = fiber
        self.f = pulse_out.f_THz
        self.t = pulse_out.t_ps

    def get_results(self):
        return self.z, self.f, self.t, self.AT, self.AW

    def get_amplitude_wavelengths(self, wavemin=None, wavemax=None, waven=None):
        '''
        Re-interpolates the AW array from evenly-spaced frequencies to
        evenly-spaced wavelengths.

        Parameters
        ----------
        wavemin : float or None
            the minimum wavelength for the re-interpolation grid.
            If None, it defaults to 0.25x the center wavelength of the pulse.
            If a float, this is the minimum wavelength of the pulse in nm
        wavemax : float or None
            the minimum wavelength for the re-interpolation grid.
            If None, it defaults to 4x the center wavelength of the pulse.
            If a float, this is the maximum wavelength of the pulse in nm
        waven : int or None
            number of wavelengths for the re-interpolation grid.
            If None, it defaults to the number of points in AW multiplied by 2
            If an int, then this is just the number of points.
        '''

        c = constants.value('speed of light in vacuum')*1e9/1e12  # c in nm/ps

        if wavemin == None:
            wavemin = 0.25 * c/self.pulse_in.centerfrequency_THz
        if wavemax == None:
            wavemax = 4.0 * c/self.pulse_in.centerfrequency_THz
        if waven== None:
            waven = self.AW.shape[1] * 2

        IW_dB = 10*log10(np.abs(self.AW)**2)  # log scale spectral intensity
        new_wls = np.linspace(wavemin, wavemax, waven)

        NEW_WLS, NEW_Z = np.meshgrid(new_wls, self.z)
        NEW_F = c/NEW_WLS

        # fast interpolation to wavelength grid, so that we can plot using
        # imshow for fast viewing. This requires Scipy > 1.6.0.
        AW_WL = scipy.ndimage.interpolation.map_coordinates(
                    np.abs(self.AW)**2, ((NEW_Z-np.min(self.z))/(self.z[1]-self.z[0]),
                         (NEW_F-np.min(self.f))/(self.f[1]-self.f[0])),
                    order=1, mode='nearest')
                    
        return new_wls, AW_WL
