import numpy as np
import cma
import concurrent.futures

from ig_vpsto.obf import OBF
from ig_vpsto.vptraj import VPTraj

# Collection of options for the VPSTO algorithm
class VPSTOOptions:
    def __init__(self, ndof):
        # Initialize with default parameters
        self.ndof = ndof
        self.vel_lim = 0.1 * np.ones(ndof)  # -vel_lim < dq < vel_lim (element-wise),
                                            # ignored if traj_duration is not None
        self.acc_lim = 1.0 * np.ones(ndof)  # -acc_lim < ddq < acc_lim (element-wise),
                                            # ignored if traj_duration is not None
        self.traj_duration = None           # duration of the trajectory (scalar), 
                                            # will be computed from vel_lim and acc_lim if None
        self.N_eval = 100                   # number of evaluation points along trajectories in cost function
        self.N_via = 5                      # number of via-points
        self.pop_size = 25                  # number of trajectories per population
        self.sigma_init = 0.5               # initial variance of via-points for CMA-ES algo
        self.max_iter = 1000                # maximum number of vpsto iterations
        self.CMA_diagonal = False           # Set to True for faster, less accurate optimization (linear complexity)
        self.multithreading = False         # Set to True for concurrently executing the cost evaluation
        self.log = False                    # Set to True for logging the optimization process
        self.verbose = True                 # Set to True for printing the optimization progress
        self.seed = None                    # Set to an integer for reproducible results

# Container for the current solution of the VPSTO algorithm
class VPSTOSolution:
    def __init__(self, options):
        self.ndof = options.ndof
        self.N_via = options.N_via
        self.log = options.log
        self.candidates = dict()
        self.candidates['pos'] = None
        self.candidates['vel'] = None
        self.candidates['acc'] = None
        self.candidates['p_via'] = None
        self.candidates['T'] = 0.0
        
        self.p_best = None # current best solution for the via-point parameters
        self.p_mean = None # current mean solution of the via-point parameters
        self.c_best = None # current best cost
        self.T_best = None # duration of the best solution

        self.w_best = None # best solution for the via-point parameters (only available after optimization)

        # log containers
        if self.log:
            self.candidates_list = []
            self.loss_list = []
            self.via_mean_list = []
            self.via_best_list = []
        
    def get_posvelacc(self, t):
        # Return the position, velocity and acceleration at time t
        # of the best available trajectory
        # t: time. Can be a scalar or a vector

        if self.w_best is None:
            print('No solution available. Run optimization first.')
            return [], [], []
                    
        obf = OBF(self.ndof)
        T = np.max([self.T_best, 1e-3])
        obf.setup_task(T * np.ones(self.N_via) / self.N_via)
        q = (obf.get_Phi(t) @ self.w_best).reshape(-1,self.ndof)
        dq = (obf.get_dPhi(t) @ self.w_best).reshape(-1,self.ndof)
        ddq = (obf.get_ddPhi(t) @ self.w_best).reshape(-1,self.ndof)
        
        return q, dq, ddq

# Main class for the VPSTO algorithm
# Usage:
#   1. Create an instance of VPSTOOptions
#   2. Set the options (optional)
#   3. Create an instance of VPSTO
#   4. Run the optimization (VPSTO.minimize(loss))
class VPSTO():
    def __init__(self, options):
        self.opt = options
        self.vptraj = VPTraj(options.ndof, 
                             options.N_eval, 
                             options.N_via,
                             options.vel_lim,
                             options.acc_lim)
        
        self.p_init = None
    
    def change_num_via(self, N_via):   
        # Change the number of via-points -> requires reinstantiation of VPTraj
        # N_via: new number of via-points
        self.opt.N_via = N_via
        self.vptraj = VPTraj(self.opt.ndof, 
                             self.opt.N_eval, 
                             self.opt.N_via,
                             self.opt.vel_lim,
                             self.opt.acc_lim)
        self.vptraj.N_via = N_via
        
    def set_initial_guess(self, p_init):
        # Set the initial guess for the via-point parameters
        # p_init: initial guess for the via-point parameters
        self.p_init = p_init

    def check_input(self, q0, dq0, qT, dqT, T):
        # Check input
        if dq0 is None:
            dq0 = np.zeros(self.opt.ndof)
        if dqT is None and T is None:
            print('Either T or dqT must be given. Setting dqT to zero.')
            dqT = np.zeros(self.opt.ndof)
        if qT is None and dqT is None:
            # qT and dqT are contained in p
            dim_x = self.opt.ndof * (self.opt.N_via+1)
            ddPhi_b = np.concatenate((self.vptraj.ddPhi[:, :self.opt.ndof],
                                      self.vptraj.ddPhi[:, -2*self.opt.ndof:-self.opt.ndof]), 
                                      axis=1)
            mu_p = - self.vptraj.ddPhi_p_qdq @ ddPhi_b @ np.concatenate((q0, dq0))
            sigma_p_chol = self.vptraj.S_qdq_chol
            sigma_p_chol_inv = self.vptraj.S_qdq_chol_inv
        elif qT is None:
            # qT is contained in p
            dim_x = self.opt.ndof * self.opt.N_via
            ddPhi_b = np.concatenate((self.vptraj.ddPhi[:, :self.opt.ndof],
                                      self.vptraj.ddPhi[:, -2*self.opt.ndof:]), 
                                      axis=1)
            mu_p = - self.vptraj.ddPhi_p_q @ ddPhi_b @ np.concatenate((q0, dq0, dqT))
            sigma_p_chol = self.vptraj.S_q_chol
            sigma_p_chol_inv = self.vptraj.S_q_chol_inv
        elif dqT is None:
            # dqT is contained in p
            dim_x = self.opt.ndof * self.opt.N_via
            ddPhi_b = np.concatenate((self.vptraj.ddPhi[:, :self.opt.ndof],
                                      self.vptraj.ddPhi[:, -3*self.opt.ndof:-self.opt.ndof]), 
                                      axis=1)
            mu_p = - self.vptraj.ddPhi_p_dq @ ddPhi_b @ np.concatenate((q0, qT, dq0))
            sigma_p_chol = self.vptraj.S_dq_chol
            sigma_p_chol_inv = self.vptraj.S_dq_chol_inv
        else:
            # qT and dqT are given
            dim_x = self.opt.ndof * (self.opt.N_via-1)
            ddPhi_b = np.concatenate((self.vptraj.ddPhi[:, :self.opt.ndof],
                                      self.vptraj.ddPhi[:, -3*self.opt.ndof:]), 
                                      axis=1)
            mu_p = - self.vptraj.ddPhi_p @ ddPhi_b @ np.concatenate((q0, qT, dq0, dqT))
            sigma_p_chol = self.vptraj.S_chol
            sigma_p_chol_inv = self.vptraj.S_chol_inv
        return dim_x, mu_p, sigma_p_chol, sigma_p_chol_inv  
        
    def minimize(self, loss, q0, dq0=None, qT=None, dqT=None, T=None):
        # Run the optimization
        # loss: loss function. Must take a dictionary of the form 
        # {'pos': q, 'vel': dq, 'acc': ddq, 'T': T}
        # as input and return a cost value for each trajectory
        # q0: initial position
        # dq0: initial velocity (optional), default: 0
        # qT: final position (optional), default: None
        # dqT: final velocity (optional), default: None
        # T: duration of the movement (optional), default: None

        # Check input and compute priors
        dim_x, mu_p, sigma_p_chol, sigma_p_chol_inv = self.check_input(q0, dq0, qT, dqT, T)

        # Initialize the solution
        sol = VPSTOSolution(self.opt)
        if self.p_init is not None:
            x_init = sigma_p_chol_inv @ (self.p_init - mu_p)
            self.p_init = None # Reset the initial guess so it is not used again
        else:
            x_init = np.zeros(dim_x)
        
        cmaes = cma.CMAEvolutionStrategy(x_init, self.opt.sigma_init, 
                                         {'CMA_diagonal': self.opt.CMA_diagonal, 
                                          'verbose': -1,
                                          'CMA_active': True,
                                          'popsize': self.opt.pop_size,
                                          'tolfun': 1e-6, 
                                          'seed': self.opt.seed})
        
        # Run the optimization for max_iter iterations
        # or until the stop criterion is met
        i = 0
        sol.c_best = np.inf
        while not cmaes.stop() and i < self.opt.max_iter:
            x_samples = np.array(cmaes.ask())
            p_samples = mu_p+(sigma_p_chol@x_samples.T).T
            if T is None:
                sol.candidates['T'] = self.vptraj.get_min_duration(p_samples, q0, dq0, qT, dqT)
            else:
                sol.candidates['T'] = T * np.ones(self.opt.pop_size)
            (sol.candidates['pos'],
             sol.candidates['vel'],
             sol.candidates['acc']) = self.vptraj.get_trajectory(p_samples,
                                                                 q0, 
                                                                 dq0, 
                                                                 qT,
                                                                 dqT,
                                                                 sol.candidates['T'])
            sol.candidates['p_via'] = p_samples
            if self.opt.multithreading is False:
                costs = loss(sol.candidates)
            else:
                costs = self.__loss_multithread(loss, sol)
            cmaes.tell(x_samples, costs)

            # Update the best solution found so far
            if np.min(costs) < sol.c_best:
                i_best = np.argmin(costs)
                sol.c_best = costs[i_best]
                sol.T_best = sol.candidates['T'][i_best]
                sol.p_best = mu_p+sigma_p_chol@cmaes.result.xbest

            sol.p_mean = mu_p+sigma_p_chol@cmaes.result.xfavorite

            # Logging the results if logging is enabled
            if self.opt.log:
                sol.candidates_list.append(p_samples)
                sol.loss_list.append(costs)
                sol.via_mean_list.append(sol.p_mean)
                sol.via_best_list.append(sol.p_best)
            
            # Print the current iteration if verbose is enabled
            if self.opt.verbose:
                print('# VP-STO iteration:', i, 'Current loss:', sol.c_best, end='\r')
            
            i += 1
        
        # Store w of final solution
        if qT is None and dqT is None:
            sol.w_best = np.concatenate((q0, sol.p_best[:-self.opt.ndof], dq0, sol.p_best[-self.opt.ndof:]))
        elif qT is None:
            sol.w_best = np.concatenate((q0, sol.p_best, dq0, dqT))
        elif dqT is None:
            sol.w_best = np.concatenate((q0, sol.p_best[:-self.opt.ndof], qT, dq0, sol.p_best[-self.opt.ndof:]))
        else:
            sol.w_best = np.concatenate((q0, sol.p_best, qT, dq0, dqT))
        
        # Print the final results if verbose is enabled
        if self.opt.verbose:
            print('VP-STO finished after', i, 'iterations with a final loss of', sol.c_best)
        
        return sol
    
    def predictive_sampling(self, loss, q, dq, qT_bias, Q, R):
        ### samples candidate trajectories and chooses the best one
        print('Using IG-VPSTO: predictive sampling')
        # Initialize the solution
        sol = VPSTOSolution(self.opt)
        dqT=np.zeros_like(dq)
        # Sample candidate trajectories, compute their loss and return the best one
        white_noise = np.random.normal(size=(self.opt.pop_size, (self.opt.N_via)*self.opt.ndof))
        # check if qT_bias is is 2 dimensional 
        print('qT_bias shape:', qT_bias.shape)
        if len(qT_bias.shape) > 1:
            # then we need to iterate over the pop_size
            p_candidates = np.zeros((self.opt.pop_size, self.opt.N_via*self.opt.ndof))
            T_candidates = np.zeros(self.opt.pop_size)
            q_traj = np.zeros((self.opt.pop_size, self.opt.N_eval, self.opt.ndof))
            dq_traj = np.zeros((self.opt.pop_size, self.opt.N_eval, self.opt.ndof))
            ddq_traj = np.zeros((self.opt.pop_size, self.opt.N_eval, self.opt.ndof))
            for i in range(self.opt.pop_size):
                _, _, _, p, T = self.vptraj.sample_trajectories(white_noise[i], q, dq0=dq, qT=qT_bias[i], dqT=dqT, Q=Q, R=R)
                p_candidates[i] = p
                T_candidates[i] = T
                (q_traj[i], dq_traj[i], ddq_traj[i]) = self.vptraj.get_trajectory(p_candidates[i], q, dq0=dq, dqT=dqT, T=T_candidates[i])
            sol.candidates['pos'] = q_traj
            sol.candidates['vel'] = dq_traj
            sol.candidates['acc'] = ddq_traj
            sol.candidates['p_via'] = p_candidates
            sol.candidates['T'] = T_candidates
        else:
            pos, vel, acc, p, T = self.vptraj.sample_trajectories(white_noise, q, dq0=dq, qT=qT_bias, 
                                                                dqT=dqT, Q=Q, R=R)

            sol.candidates['T'] = T * np.ones(self.opt.pop_size)
            (sol.candidates['pos'],
            sol.candidates['vel'],
            sol.candidates['acc']) = self.vptraj.get_trajectory(p, q, dq0=dq, dqT=dqT, T=sol.candidates['T'])
            sol.candidates['p_via'] = p
        if self.opt.multithreading is False:
            costs = loss(sol.candidates)
        else:
            costs, rollouts, max_ig_indices = self.__loss_multithread(loss, sol)

        # Update the best solution 
        # if np.min(costs) < sol.c_best:
        i_best = np.argmin(costs)
        sol.rollouts = rollouts
        sol.max_ig_idx_best = max_ig_indices[i_best]
        sol.max_ig_idx = max_ig_indices
        sol.costs = costs
        sol.i_best = i_best
        sol.c_best = costs[i_best]
        sol.T_best = sol.candidates['T'][i_best]
        sol.p_best = sol.candidates['p_via'][i_best]
        return sol
    
    def __call_loss_multithreading(self, loss, candidate, costs, rollouts, max_ig_indices, idx):
        costs[idx], rollouts[idx], max_ig_indices[idx] = loss(candidate)
        
    def __loss_multithread(self, loss, sol):
        pop_size = len(sol.candidates['T'])
        costs = np.empty(pop_size)
        rollouts = np.empty(pop_size, dtype=object)
        max_ig_indices = np.empty(pop_size, dtype=int)
        candidates = []
        for i in range(pop_size):
            candidates.append({'pos': sol.candidates['pos'][i],
                               'vel': sol.candidates['vel'][i],
                               'acc': sol.candidates['acc'][i],
                               'T': sol.candidates['T'][i]})
        with concurrent.futures.ThreadPoolExecutor(max_workers=pop_size) as executor:
            futures = []
            for i in range(pop_size):
                futures.append(executor.submit(
                    self.__call_loss_multithreading, loss, 
                    candidates[i], costs, rollouts, max_ig_indices, i))
            for future in concurrent.futures.as_completed(futures):
                future.result()
        return costs, rollouts, max_ig_indices