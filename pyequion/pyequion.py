import logging
import os

import numpy as np
from scipy import optimize

from . import core
from . import reactions_species_builder as rbuilder
from .reactions_species_builder import display_reactions
from . import utils
from . import utils_api
from .core import DEFAULT_DB_FILES, EquilibriumSystem, SolutionResult
from .properties_utils import pCO2_ref, solve_with_exception
from .utils import ClosingEquationType, get_dissociating_ions
from . import activity_coefficients
from . import reactions_constants #this is outdated, I am only using the logK_H ...
from .activity_coefficients import TypeActivityCalculation,\
    setup_log_gamma_ideal, calc_log_gamma_ideal,\
    setup_log_gamma_bdot, calc_log_gamma_dh_bdot,\
    setup_log_gamma_bdot_mean_activity_neutral, calc_log_gamma_dh_bdot_mean_activity_neutral,\
    setup_log_gamma_pitzer, calc_log_gamma_pitzer

from .core import save_jacobian_of_res_to_file, save_res_to_file

def solve_solution(comp_dict, reaction_system=None, TC=25.0,
    close_type=None, carbon_total=0.0,
    initial_feed_mass_balance=None, x_guess=None,
    element_mass_balance=None,
    allow_precipitation=False,
    solid_equilibrium_phases=None,
    activity_model_type=TypeActivityCalculation.DEBYE,
    setup_log_gamma_func=None,
    calc_log_gamma=None,
    calculate_solubility=None,
    vapour_equilibrium_phase=None, #Aborted - Understand this equilibrium better
    co2_partial_pressure=core.pCO2_ref,
    fugacity_calculation='ideal', #'ideal'or 'pr', maybe a UDF
    jac=None
    ):
    if reaction_system is None:
        feed_compounds = [k for k in comp_dict.keys()]
        comps_vals = [v*1e-3 for v in comp_dict.values()]
    else:
        feed_compounds = reaction_system.feed_compounds
        comps_vals = [
            comp_dict[feed]*1e-3 if feed in comp_dict else np.nan
            for feed in feed_compounds
        ]

    # close_type value used only if reaction_system is None:
    if reaction_system is not None:
        if close_type is not None:
            logging.warning('solve_solution with reaction_system not None - Using from reaction_system')
        close_type = reaction_system.closing_equation_type
    else:
        close_type = close_type if  close_type is not None else ClosingEquationType.NONE

    ## Neutrality Checking:
    if not np.any(np.isnan(comps_vals)):
        charges = np.array([rbuilder.get_charge_of_specie(tag) for tag in feed_compounds])
        check_neutrality = np.sum(charges * comps_vals)
        assert np.isclose(check_neutrality, 0.0), 'Error: Feed Neutrality should be zero!'

    # if reaction_system is None: FIXME
    #     feed_compounds = [k for k in comp_dict.keys()]
    #     comps_vals = np.array([v*1e-3 for v in comp_dict.values()])
    # else:
    #     feed_compounds = reaction_system.feed_compounds
    #     if comp_dict:
    #         comps_vals = np.array([
    #             comp_dict[feed]*1e-3 if feed in comp_dict else np.nan # WAS NAN -> DO NOT NOW IF WILL BREAK THINGS
    #             for feed in feed_compounds
    #         ])
    #     else:
    #         comps_vals = []

    args = get_args_from_comps_dict(TC, comps_vals, close_type, co2_partial_pressure, carbon_total)
        # if vapour_equilibrium_phase:
        #     args = (val_arr, TK, vapour_equilibrium_phase['CO2(g)'])
        # else:
        #     args = (val_arr, TK, np.nan)

    # if is_open:
    #     closing_equation_type = ClosingEquationType.OPEN
    # else:
    #     closing_equation_type = ClosingEquationType.CARBON_TOTAL

    if reaction_system is None:
        sys_eq = create_equilibrium(feed_compounds,
            close_type, element_mass_balance,
            initial_feed_mass_balance)
    else:
        sys_eq = reaction_system

    # if fugacity_calculation == 'pr':
    #     sys_eq.fugacity_calculation = fugacity_calculation #WILL FAIL IN NUMBA

    solution = solve_equilibrium(sys_eq, args=args, x_guess=x_guess,
        activity_model_type=activity_model_type, #_MEAN,
        allow_precipitation=allow_precipitation,
        solid_equilibrium_phases=solid_equilibrium_phases,
        setup_log_gamma_func=setup_log_gamma_func,
        calc_log_gamma=calc_log_gamma,
        calculate_solubility=calculate_solubility,
        vapour_equilibrium_phase=vapour_equilibrium_phase,
        fugacity_calculation=fugacity_calculation,
        jac=jac
    )
    return solution

def solve_solution_pre_loaded(comp_dict, reaction_system,
    x_guess,
    TC=25.0,
    close_type=None, carbon_total=0.0,
    calc_log_gamma=activity_coefficients.calc_log_gamma_dh_bdot,
    initial_feed_mass_balance=None,
    co2_partial_pressure=core.pCO2_ref,
    user_solver_function=None,
    # fugacity_calculation='ideal', #'ideal'or 'pr', maybe a UDF
    jac=None
    ):

    comps_vals = [v*1e-3 for v in comp_dict.values()]
    args = get_args_from_comps_dict(TC, comps_vals, close_type, co2_partial_pressure, carbon_total)
    args_calc_gamma = (args, calc_log_gamma)
    if user_solver_function:
        solver_function = user_solver_function
    else:
        solver_function = solve_with_exception
    fsol = solver_function(reaction_system.residual,
        x_guess, args_calc_gamma,
        jac=jac
    )
    solution = reaction_system.calculate_properties(fsol.success)
    return solution

def setup_system_for_direct_run(reaction_system,
    activities_db_file_name=None,
    setup_log_gamma_func=activity_coefficients.setup_log_gamma_bdot,
    TC=25,
    ):
    if not activities_db_file_name:
        activities_db_file_name = DEFAULT_DB_FILES['species']
    species_activity_db = utils.load_from_db(activities_db_file_name)
    setup_log_gamma_func(reaction_system, TC+273.15, species_activity_db)

def get_args_from_comps_dict(TC, comps_vals, close_type, co2_partial_pressure, carbon_total):
    TK = TC + 273.15
    val_arr = np.atleast_1d(comps_vals)
    if close_type == ClosingEquationType.OPEN:
        args = (val_arr, TK, co2_partial_pressure)
    elif close_type == ClosingEquationType.CARBON_TOTAL:
        args = (val_arr, TK, carbon_total*1e-3)
    else:
        args = (val_arr, TK, np.nan)
    return args

def create_equilibrium(feed_compounds,
    closing_equation_type : ClosingEquationType=ClosingEquationType.NONE,
    element_mass_balance=None, initial_feed_mass_balance=None,
    allow_precipitation=False, return_intermediaries=False,
    # activity_model_type: TypeActivityCalculation=TypeActivityCalculation.DEBYE,
    fixed_elements=None,
    database_files=DEFAULT_DB_FILES,
    solid_reactions_in=None,
    possible_aqueous_reactions_in=None,
    possible_solid_reactions_in=None,
    closing_equation_element=None,
    vapour_equilibrium_phase=None, #if True allow the system to form vapour phase, if dict specify the vapour phases (TODO)
    fugacity_calculation='ideal'
    ):

    initial_comp, known_tags = rbuilder.get_initial_comp_and_known_tags_from_ini_config(
            feed_compounds, initial_feed_mass_balance, closing_equation_type, fixed_elements
    )

    if possible_aqueous_reactions_in is None:
        reactionsListSolutions = utils.load_from_db(database_files['solutions'])
    else:
        reactionsListSolutions = possible_aqueous_reactions_in
    # Append Irreversible (not listed in phreeqc.dat)
    reactionsListSolutions += rbuilder.reactionsListIrreversible

    if vapour_equilibrium_phase or closing_equation_type == ClosingEquationType.OPEN:
        reactionsListSolutions += rbuilder.reactionsListVapourPhase

    reactionList_ = reactionsListSolutions
    if allow_precipitation:
        if solid_reactions_in is None:
            logging.debug('''
                Creating system with allow_precipitation.
                Solid is being retrieve from database (all possible solids may precipitate)
            ''')
            reactionsListPhase = utils.load_from_db(database_files['phases'])
        else:
            logging.debug('''
                Creating system with allow_precipitation.
                Using the provided list of solid reactions.
            ''')
            reactionsListPhase = solid_reactions_in
        reactionList_ += reactionsListPhase

    reactions, species = rbuilder.get_reactions_species_from_initial_configs(
        allow_precipitation, initial_comp, closing_equation_type, reactionList_)
    # del reactions[7] #FIXME
    # del reactions[7]
    # species.discard('CaCO3(s)__Aragonite')
    # species.discard('CaCO3(s)__Vaterite')

    sys_eq, species, reactions, dict_indexes_species_conv, \
            engine_idxs, mb_list_engine, known_specie, dic_tuple \
        = rbuilder.create_equilibrium_from_reaction_and_species(
        reactions, species,
        known_tags, element_mass_balance, feed_compounds,
        initial_feed_mass_balance, closing_equation_type, True,
        fixed_elements, database_files, possible_solid_reactions_in,
        closing_equation_element,
        allow_precipitation,
        solid_reactions_in, #JUST TO SAVE FOR SERIALIZATION
        # fugacity_calculation,
    )


    if return_intermediaries:
        return sys_eq, species, reactions, dict_indexes_species_conv, \
            engine_idxs, mb_list_engine, known_specie, dic_tuple
    else:
        return sys_eq

def solve_equilibrium(reaction_system, x_guess=None, args=None,
    jac=None, ret_fsol=False,
    setup_log_gamma_func=None,
    calc_log_gamma=None,
    activity_model_type=TypeActivityCalculation.DEBYE,
    activities_db_file_name=None,
    allow_precipitation=False,
    solid_equilibrium_phases=None,
    calculate_solubility=None,
    vapour_equilibrium_phase=None, #use types, None or ('phase-name', 'feed-comp-to-release')
    fugacity_calculation='ideal',
    ):
    """Lower function than solve_solution

    Parameters
    ----------
    reaction_system : [type]
        [description]
    x_guess : [type], optional
        [description], by default None
    args : [type], optional
        [description], by default None
    jac : [type], optional
        [description], by default None
    ret_fsol : bool, optional
        [description], by default False
    setup_log_gamma_func : [type], optional
        [description], by default None
    calc_log_gamma : [type], optional
        [description], by default None
    activity_model_type : [type], optional
        [description], by default TypeActivityCalculation.DEBYE
    activities_db_file_name : [type], optional
        [description], by default None
    allow_precipitation : bool, optional
        [description], by default False
    solid_equilibrium_phases : [type], optional
        [description], by default None
    calculate_solubility : [type], optional
        [description], by default None
    vapour_equilibrium_phase : [type], optional
        [description], by default None

    Returns
    -------
    [type]
        [description]

    Raises
    ------
    ValueError
        [description]
    ValueError
        [description]
    ValueError
        [description]
    ValueError
        [description]
    ValueError
        [description]
    ValueError
        [description]
    """
    if allow_precipitation and jac is not None:
        raise ValueError('Unsupported feature: jacobian with allow precipitation.')
    if not activities_db_file_name:
        activities_db_file_name = DEFAULT_DB_FILES['species']
    if calculate_solubility and allow_precipitation:
        raise ValueError('Cannot use allow_precipitation with calculate_solubility. Please select just one (TODO: changed that?)')

    if fugacity_calculation == 'pr':
        reaction_system.fugacity_calculation = fugacity_calculation #WILL FAIL IN NUMBA

    # if activity_model_type is not None:
        #using the pre-defined activities model
    # if fugacity_calculation == 'pr':
    #     setup_log_gamma_func, calc_log_gamma = core.pengrobinson_co2_activity_logic(
    #         activity_model_type, setup_log_gamma_func, calc_log_gamma)
    # else:
    setup_log_gamma_func, calc_log_gamma = core.default_activity_logic(
            activity_model_type, setup_log_gamma_func, calc_log_gamma, fugacity_calculation)

    adjust_sys_for_pengrobinson(fugacity_calculation, reaction_system, args)

    species_activity_db = utils.load_from_db(activities_db_file_name)
    c_feed, TK = args[0:2]
    setup_log_gamma_func(reaction_system, TK, species_activity_db, c_feed)

    if x_guess is None:
        x_guess_use = rbuilder.get_guess_from_ideal_solution(reaction_system, args, jac, activities_db_file_name)
    else:
        x_guess_use = x_guess
    args_calc_gamma = (args, calc_log_gamma)

    # if vapour_equilibrium_phase:


    #     pass

    # Solve system:
    if not calculate_solubility:
        if allow_precipitation: #In this first solution, ignore the initial guess
            x_guess_use = rbuilder.get_guess_from_ideal_solution(reaction_system, args, jac, activities_db_file_name)
        fsol = solve_with_exception(reaction_system.residual,
            x_guess_use, args_calc_gamma,
            jac=jac
        )
        solution = reaction_system.calculate_properties(fsol.success)

    if not calculate_solubility and (not allow_precipitation):
        if ret_fsol:
            return solution, fsol, reaction_system
        return solution

    if calculate_solubility:
        #ONLY FOR FEED FORMULA === PRECIPITATION FORMULA - Come back into this issue
        phase_name = calculate_solubility[0]
        feed_comp_name = calculate_solubility[1]
        current_possible_solids = [r_solid.type for r_solid in reaction_system.solid_reactions_but_not_equation]
        if phase_name not in current_possible_solids:
            raise ValueError(f'Value provided for the solid phase is not presented in the possible solids: {current_possible_solids}')

        # Create the system in precipitation mode with the desired phase
        solids_phases_included = [r_solid for r_solid in reaction_system.solid_reactions_but_not_equation if r_solid.type == phase_name]
        reacs_conv_solid_precip = rbuilder.conv_reaction_engine_to_db_like(solids_phases_included)
        sys_eq_precip = transform_system_to_new_solids_reactions(reaction_system, reacs_conv_solid_precip)

        adjust_sys_for_pengrobinson(reaction_system.fugacity_calculation, sys_eq_precip, args)

        # Get the index of the solid concentration from the list of species (no longer an unknown)
        idx_replace_specie = [i for i, sp in enumerate(sys_eq_precip.species) if phase_name in sp.name][0]
        sys_eq_precip.set_index_solubility_calculation(idx_replace_specie)

        # Check the constraint for this GAMBIARRA:
        name_aux = sys_eq_precip.species[idx_replace_specie].name.split('__')[0][0:-3]
        if name_aux != feed_comp_name:
            raise ValueError('Work in Progress: solid phane name for solubility calculation should match the feed component name... TODO FIX IT!')

        # Replate the position with the feed as an unknown
        sys_eq_precip.species[idx_replace_specie].name = feed_comp_name
        sys_eq_precip.species[idx_replace_specie].phase = 2 #FIXME gambiarrows to reuse the former solid species (is used in the solid reaction!)
        # Get the index for the feed component of interest
        # idx_feed = reaction_system.feed_compounds.index('NaCl')
        for mb in sys_eq_precip.mass_balances:
            try:
                idx_in_idx_species = np.where(mb.idx_species == idx_replace_specie)[0][0]
                mb.stoic_coefs[idx_in_idx_species] *= -1 #Multiplying by minus one to modify the mass balance (the feed will be zero)
                mb.feed_is_unknown = True
            except IndexError:
                pass


        pass

    # If allow_precipitation: check precipitation candidates
    if allow_precipitation: #read solid reactions from db
        if not solid_equilibrium_phases:
            logging.warning('''Allow precipitation is ON and solid phase are not provided.
            The software will equilibrate with the phase with higher saturation index.
                Is recommended to set the solid phases which should be equilibrated.''')
            sys_eq_precip = modify_system_for_precipitation(solution, reaction_system)
        else:
            current_possible_solids = [r_solid.type for r_solid in reaction_system.solid_reactions_but_not_equation]

            # Check if solid name is in the possible solids to be formed:
            for solid_name in solid_equilibrium_phases:
                if solid_name not in current_possible_solids:
                    raise ValueError(f'Value provided for the solid phase is not presented in the possible solids: {current_possible_solids}')

            solids_phases_included = [r_solid for r_solid in reaction_system.solid_reactions_but_not_equation if r_solid.type in solid_equilibrium_phases]
            reacs_conv_solid_precip = rbuilder.conv_reaction_engine_to_db_like(solids_phases_included)
            sys_eq_precip = transform_system_to_new_solids_reactions(reaction_system, reacs_conv_solid_precip)

        adjust_sys_for_pengrobinson(reaction_system.fugacity_calculation, sys_eq_precip, args)

    rbuilder.setup_log_gamma(sys_eq_precip, args[1], args[0],
        setup_log_gamma_func, activities_db_file_name)

    if x_guess is None:
        x_guess_use = rbuilder.get_guess_from_ideal_solution(sys_eq_precip, args, jac, activities_db_file_name)
    else:
        x_guess_use = x_guess
    fsol = solve_with_exception(sys_eq_precip.residual,
        x_guess_use, args_calc_gamma,
        jac=jac
    )
    solution_precip = sys_eq_precip.calculate_properties(fsol.success)
    if ret_fsol:
        return solution_precip, fsol, sys_eq_precip
    return solution_precip

def adjust_sys_for_pengrobinson(fugacity_calculation, reaction_system, args):
    if fugacity_calculation == 'pr': #ugly, to inject the P at p_scalar (is the same P used in args extra)
        if reaction_system.closing_equation_type != ClosingEquationType.OPEN:
            raise ValueError('PENGRobinson only for OPEN case at this point')
        vapours = [sp for sp in reaction_system.species if '(g)' in sp.name]
        for gas in vapours:
            gas.p_scalar['P'] = args[2]
        pass
    return

    # if ret_fsol:
    #     return solution, fsol
    # return solution

def modify_system_for_precipitation(solution, reaction_system):
    solids_will_precip = [solid for solid, si in solution.saturation_index.items()
        if si > 0.0
    ]
    reac_solid_precip = [[reac for reac in reaction_system.solid_reactions_but_not_equation
        if reac.type == solid_name][0]
        for solid_name in solids_will_precip
    ]
    ## Check for polymorphs - only the most stable will precipitate
    d_solids = rbuilder.check_polymorphs_in_reaction(reac_solid_precip, solution.saturation_index)

    ## Convert Reaction Engine to db_reaction:
    reac_solid_precip_no_polymorph = [reac_solid_precip[i] for _, i in d_solids.values()]
    reacs_conv_solid_precip = rbuilder.conv_reaction_engine_to_db_like(reac_solid_precip_no_polymorph)

    # Use only the aqueous and solid reaction from previous system (does not use database for liquid)
    sys_eq_precip = transform_system_to_new_solids_reactions(reaction_system, reacs_conv_solid_precip)
    return sys_eq_precip

def print_solution(solution, conc_and_activity=False):
    "Print information of equilibrium solution results."

    print('Solution Results:')
    print('\tpH = {:.5f}'.format(solution.pH))
    print('\tsc = {:.5f}uS/cm'.format(solution.sc*1e6))
    print('\tI = {:.5f}mmol/L'.format(solution.I*1e3))
    print('\tDIC = {:.5f}mmol/L'.format(solution.DIC*1e3))
    if solution.saturation_index:
        print('Saturation Index:')
        [print(f'\t{k}: {v}') for k, v in solution.saturation_index.items()]
    if solution.ionic_activity_prod:
        print('Ionic Activity Product:')
        [print(f'\t{k}: {v}') for k, v in solution.ionic_activity_prod.items()]
    if solution.preciptation_conc:
        print('Precipitation concentration:')
        [print(f'\t{k}: {v*1e3} mM') for k, v in solution.preciptation_conc.items()]
    if solution.index_solubility_calculation > 0:
        print('Calculated Solubility (mM): ')
        # value = solution.c_molal[solution.index_solubility_calculation]*1e3
        key,value = list(solution.calculated_solubility.items())[0]
        print(f'\t{key}: {value*1e3}')
    if conc_and_activity:
        print('Concentrations [mM]:')
        [print(f'\t{name}: {c*1e3} mM')
            for name,c in zip(solution.specie_names, solution.c_molal)]
        # print(solution.c_molal)
        print('Activity Coefficients:')
        [print(f'\t{name}: {g}')
            for name,g in zip(solution.specie_names, solution.gamma)]
        # print(solution.gamma)
    pass

def equilibrate_phase(sys_eq, phase_name,
    feed_idx_adjusted, args, limit=None, kw_solv_eq={}):
    "Halted"

    def eq_res(x):
        c_feed = args[0]
        c_feed[feed_idx_adjusted] = x #MUTTABLE ARRAY -> Update args
        new_arg = (c_feed, args[1], args[2])
        solution = solve_equilibrium(sys_eq, args=new_arg, **kw_solv_eq)
        si = solution.saturation_index[phase_name]
        # print('x', x, 'si', si,
        #     'K+', solution.c_molal[solution.idx['K+']],
        #     # 'Na+', solution.c_molal[solution.idx['Na+']]
        #     'Na+', get_total_element(solution, 'Na')
        # )
        return si - 0.0

    if limit is None:
        c_feed = args[0]
        limit = [0.01e-3, c_feed[feed_idx_adjusted]]
    fsol = optimize.root_scalar(eq_res, bracket=limit, xtol=1e-8)

    if not fsol.converged:
        raise RuntimeError('Failed to converge in equilibrate_phase')
    return fsol.root

def get_total_element(solution, element, get_which_tags=False):
    a, d_sp = rbuilder.get_species_indexes_matching_element(
        solution.specie_names, element, solution.idx)
    m_tot = solution.c_molal[d_sp].sum()

    if get_which_tags:
        return m_tot, a
    return m_tot

def get_mean_activity_coeff(solution: SolutionResult, tagCompound: str):

    tags_coefs = utils.get_dissociating_ions_plain_reactions(tagCompound, solution.reactions)

    aux = 1.0
    stoic_sum = 0.0
    for tag, coef in tags_coefs:
        if not tag[0].isupper():
            continue
        gamma = solution.gamma[solution.idx[tag]]
        c = np.abs(coef)
        aux *= gamma**c
        stoic_sum +=c
        # sp_ion = species[solution.idx[tag]]
        # aux += stoic[0]*sp_ion.logg
    g_mean = aux**(1/stoic_sum)
    # gammaM = solution.gamma[solution.idx[tagM]]
    # gammaX = solution.gamma[solution.idx[tagX]]

    return g_mean

def get_activity(solution: SolutionResult, tagCompound: str):
    i = solution.idx[tagCompound]
    c = solution.c_molal[i]
    g = solution.gamma[i]
    act = c*g
    return act



###########################################################
###########################################################
# Internal functions (high level auxiliaries)
###########################################################
###########################################################

def transform_system_to_new_solids_reactions(reaction_system, reacs_conv_solid_precip):
    # Use only the aqueous and solid reaction from previous system (does not use database for liquid)
    possible_aqueous_reactions_in = rbuilder.conv_reaction_engine_to_db_like(reaction_system.reactions)
    possible_solid_reactions_in = rbuilder.conv_reaction_engine_to_db_like(reaction_system.solid_reactions_but_not_equation)

    # dictDbFiles = {}
    # for k, v in reaction_system.database_files.items():
    #     dictDbFiles[k] = v

    ## Create new ReactionSystem with precipitation enabled
    sys_eq_precip = create_equilibrium( #Fixme- reaction aqueous from converted
        list(reaction_system.feed_compounds),
        reaction_system.closing_equation_type,
        list(reaction_system.element_mass_balance),
        list(reaction_system.initial_feed_mass_balance),
        True,
        False,
        list(reaction_system.fixed_elements),
        # dictDbFiles, #CHECK ME, IM REMOVING DATABASE FROM A FILE...
        solid_reactions_in=reacs_conv_solid_precip,
        possible_aqueous_reactions_in=possible_aqueous_reactions_in,
        possible_solid_reactions_in=possible_solid_reactions_in,
        # fugacity_calculation=reaction_system.fugacity_calculation
    )
    sys_eq_precip.fugacity_calculation = reaction_system.fugacity_calculation
    return sys_eq_precip