# -*- coding: utf-8 -*-
"""
.. module:: pytfa
   :platform: Unix, Windows
   :synopsis: Thermodynamic constraints for Flux-Based Analysis of reactions

.. moduleauthor:: pyTFA team

Thermodynamic model class and methods definition


"""

import re
from collections import defaultdict
from copy import deepcopy
from math import log

import optlang.interface
import pandas as pd
from cobra import Model
from cobra.core import Solution, DictList
from optlang.exceptions import SolverError

from . import std
from .thermo import get_debye_huckel_b
from .thermo import MetaboliteThermo, calcDGR_cues, calcDGtpt_rhs
from .utils import check_reaction_balance, check_transport_reaction, \
    find_transported_mets
from ..optim.constraints import SimultaneousUse, NegativeDeltaG, \
    BackwardDeltaGCoupling, ForwardDeltaGCoupling, BackwardDirectionCoupling, \
    ForwardDirectionCoupling, ReactionConstraint, MetaboliteConstraint, \
    DisplacementCoupling
from ..optim.utils import get_primal
from ..optim.variables import ThermoDisplacement, DeltaGstd, DeltaG, \
    ForwardUseVariable, BackwardUseVariable, LogConcentration, GenericVariable, \
    ReactionVariable, MetaboliteVariable
from ..utils import numerics
from ..utils.logger import get_bistream_logger
from ..utils.str import camel2underscores

BIGM = numerics.BIGM
BIGM_THERMO = numerics.BIGM_THERMO
BIGM_DG = numerics.BIGM_DG
BIGM_P = numerics.BIGM_P
EPSILON = numerics.EPSILON
MAX_STOICH = 10


class ThermoModel(Model):
    """
    A class representing a model with thermodynamics information

    """

    def __init__(self, thermo_data, model, name=None,
                 temperature=std.TEMPERATURE_0,
                 min_ph=std.MIN_PH,
                 max_ph=std.MAX_PH):

        """

        :param float temperature: the temperature (K) at which to perform the calculations
        :param dict thermo_data: The thermodynamic database
        :type temperature: float
        """

        Model.__init__(self, deepcopy(model), name)

        self.TEMPERATURE = temperature
        self.thermo_data = thermo_data
        self.thermo_unit = thermo_data['units']
        self.reaction_cues_data = thermo_data['cues']
        self.compounds_data = thermo_data['metabolites']
        self.Debye_Huckel_B = get_debye_huckel_b(temperature)
        self.parent = model

        self.logger = get_bistream_logger('thermomodel_' + str(self.name))

        # Compute internal values to adapt the the thermo_unit provided
        if self.thermo_unit == "kJ/mol":
            self.GAS_CONSTANT = 8.314472 / 1000  # kJ/(K mol)
            self.Adjustment = 1
        else:
            self.GAS_CONSTANT = 1.9858775 / 1000  # Kcal/(K mol)
            self.Adjustment = 4.184

        TEMPERATURE = temperature  # K

        self.RT = self.GAS_CONSTANT * self.TEMPERATURE

        # CONSTANTS
        self.MAX_pH = max_ph
        self.MIN_pH = min_ph

        self._var_dict = dict()
        self._cons_dict = dict()

        self.logger.info('# Model initialized with units {} and temperature {} K'  \
                    .format(self.thermo_unit, self.TEMPERATURE))

    def normalize_reactions(self):
        """
        Find reactions with important stoichiometry and normalizes them
        :return:
        """

        self.logger.info('# Model normalization')

        for this_reaction in self.reactions:
            metabolites = this_reaction.metabolites
            max_stoichiometry = max(metabolites.values())
            if max_stoichiometry > MAX_STOICH:
                new_metabolites = {k:-v+v/max_stoichiometry \
                                   for k,v in metabolites.items()}
                this_reaction.add_metabolites(new_metabolites)
            else:
                continue

    def _prepare_metabolite(self,met):
        """

        :param met:
        :return:
        """

        # Get the data about the compartment of the metabolite
        if not met.compartment in self.compartments:
            raise Exception("Compartment not found in model : "
                            + met.compartment)

        CompartmentpH = self.compartments[met.compartment]['pH']
        CompartmentionicStr = self.compartments[met.compartment]['ionicStr']

        # Which index of the reaction DB do you correspond to ?
        if not 'seed_id' in met.annotation:
            raise Exception("seed_id missing for " + met.name)

        if not met.annotation['seed_id'] in self.compounds_data:
            self.logger.warning("Metabolite {} ({}) not present in thermoDB"
                  .format(met.annotation['seed_id'], met.name))
            metData = None
        else:
            metData = self.compounds_data[met.annotation['seed_id']]

        met.thermo = MetaboliteThermo(metData,
                                      CompartmentpH,
                                      CompartmentionicStr,
                                      self.TEMPERATURE,
                                      self.MIN_pH,
                                      self.MAX_pH,
                                      self.Debye_Huckel_B,
                                      self.thermo_unit)

    def _prepare_reaction(self,reaction):
        DeltaGrxn = 0
        DeltaGRerr = 0
        proton_of = self._proton_of

        # identifying the reactants
        if len(reaction.metabolites) == 1:
            NotDrain = False
        else:
            NotDrain = True

        # Initialize a dictionnary where we will put our data - FIXME : Create a thermo object ?
        reaction.thermo = {'isTrans': False}

        # also check if rxn and metabolite compartments match
        reaction.compartment = None
        for met in reaction.metabolites:
            if reaction.compartment == None:
                reaction.compartment = met.compartment
            elif met.compartment != reaction.compartment:
                reaction.compartment = 'c'

        # Make sure the reaction is balanced...

        balanceResult = check_reaction_balance(reaction,
                                               (proton_of[reaction.compartment]
                                              if reaction.compartment in proton_of
                                              else None))

        # Also test if this is a transport reaction
        reaction.thermo['isTrans'] = check_transport_reaction(reaction)
        # Make sure we have correct thermo values for each metabolites
        correctThermoValues = True

        for met in reaction.metabolites:
            if met.thermo.deltaGf_std > 0.9 * BIGM_DG:
                correctThermoValues = False
                break

        if (not NotDrain
            or not correctThermoValues
            or len(reaction.metabolites) >= 100
            or balanceResult in ['missing atoms', 'drain flux']):

            self.logger.warning('{} : thermo constraint NOT created'.format(reaction.id))
            reaction.thermo['computed'] = False
            reaction.thermo['deltaGR'] = BIGM_DG
            reaction.thermo['deltaGRerr'] = BIGM_DG

        else:
            self.logger.debug('{} : thermo constraint created'.format(reaction.id))
            reaction.thermo['computed'] = True

            if reaction.thermo['isTrans']:
                (rhs, breakdown) = calcDGtpt_rhs(reaction,
                                                 self.compartments,
                                                 self.thermo_unit)

                reaction.thermo['deltaGR'] = rhs

                reaction.thermo['deltaGrxn'] = breakdown['sum_deltaGFis']
            else:
                for met in reaction.metabolites:
                    if (met.formula != 'H'
                        or ('seed_id' in met.annotation
                            and met.annotation['seed_id'] != 'cpd00067')):
                        DeltaGrxn += reaction.metabolites[
                                         met] * met.thermo.deltaGf_tr
                        DeltaGRerr += abs(reaction.metabolites[
                                              met] * met.thermo.deltaGf_err)

                reaction.thermo['deltaGR'] = DeltaGrxn

            (tmp1, DeltaGRerr, tmp2, tmp3) = calcDGR_cues(reaction,
                                                          self.reaction_cues_data)

            if DeltaGRerr == 0:
                DeltaGRerr = 2  # default value for DeltaGRerr

            reaction.thermo['deltaGRerr'] = DeltaGRerr

    def prepare(self):
        """ Prepares a COBRA toolbox model for TFBA analysis by doing the following:

           1. checks if a reaction is a transport reaction
           2. checks the ReactionDB for Gibbs energies of formation of metabolites
           3. computes the Gibbs energies of reactions

        """

        self.logger.info('# Model preparation starting...')

        # Number of reactions
        num_rxns = len(self.reactions)

        # Number of metabolites
        num_mets = len(self.metabolites)

        for i in range(num_mets):
            met = self.metabolites[i]
            self._prepare_metabolite(met)


        # And now, reactions !

        self.logger.debug('computing reaction thermodynamic data')

        # Look for the proton metabolite...
        proton = {}
        for i in range(num_mets):
            if (self.metabolites[i].formula == 'H'
                or ('seed_id' in self.metabolites[i].annotation
                    and self.metabolites[i].annotation[
                        'seed_id'] == 'cpd00067')):
                proton[self.metabolites[i].compartment] = self.metabolites[i]

        if len(proton) == 0:
            raise Exception("Cannot find proton")
        else:
            self._proton_of = proton

        # Iterate over each reaction
        for i in range(num_rxns):
            reaction = self.reactions[i]
            self._prepare_reaction(reaction)

        self.logger.info('# Model preparation done.')


    def _convert_metabolite(self, met, add_potentials, verbose):
        """
        Given a metabolite, proceeds to create the necessary variables and
        constraints for thermodynamics-based modeling

        :param met:
        :return:
        """

        if add_potentials:
            P_lb = -BIGM_P  # kcal/mol
            P_ub = BIGM_P  # kcal/mol

        # exclude protons and water and those without deltaGF
        # P_met: P_met - RT*LC_met = DGF_met
        metformula = met.formula
        metDeltaGF = met.thermo.deltaGf_tr
        metComp = met.compartment
        metLConc_lb = log(self.compartments[metComp]['c_min'])
        metLConc_ub = log(self.compartments[metComp]['c_max'])
        Comp_pH = self.compartments[metComp]['pH']
        LC = None

        if metformula == 'H2O':
            LC = self.add_variable(LogConcentration, met, lb=0, ub=0)

        elif metformula == 'H':
            LC = self.add_variable(
                              LogConcentration,
                              met,
                              lb=log(10 ** -Comp_pH),
                              ub=log(10 ** -Comp_pH))

        elif ('seed_id' in met.annotation
              and met.annotation['seed_id'] == 'cpd11416'):
            # we do not create the thermo variables for biomass metabolite
            pass

        elif metDeltaGF < 10 ** 6:
            if verbose:
                self.logger.debug('generating thermo variables for {}'.format(met.id))
            LC = self.add_variable( LogConcentration,
                                    met,
                                    lb=metLConc_lb,
                                    ub=metLConc_ub)

            if add_potentials:
                P = self.add_variable( 'P_' + met.id, P_lb, P_ub)
                self.P_vars[met] = P
                # Formulate the constraint
                expr = P - self.RT * LC
                self.add_constraint(
                               LogConcentration,
                               'P_' + met.id,
                               expr,
                               metDeltaGF,
                               metDeltaGF)

        else:
            self.logger.warning('NOT generating thermo variables for {}'.format(met.id))

        if LC != None:
            # Register the variable to find it more easily
            self.LC_vars[met] = LC

    def _convert_reaction(self, rxn,
                          add_potentials,
                          add_displacement,
                          verbose):
        """

        :param rxn:
        :param add_potentials:
        :param add_displacement:
        :param verbose:
        :return:
        """

        RT = self.RT

        DGR_lb = -BIGM_THERMO  # kcal/mol
        DGR_ub = BIGM_THERMO  # kcal/mol

        epsilon = self.solver.configuration.tolerances.feasibility

        # Is it a water transport reaction ?
        H2OtRxns = False
        if rxn.thermo['isTrans'] and len(rxn.reactants) == 1:
            if rxn.reactants[0].annotation['seed_id'] == 'cpd00001':
                H2OtRxns = True

        # Is it a drain reaction ?
        NotDrain = len(rxn.metabolites) > 1

        forward = rxn.forward_variable
        reverse = rxn.reverse_variable

        # if the reaction is flagged with rxnThermo, and it's not a H2O
        # transport, we will add thermodynamic constraints
        if rxn.thermo['computed'] and not H2OtRxns:
            if verbose:
                self.logger.debug('generating thermo constraint for {}'.format(rxn.id))

            # add the delta G as a variable
            DGR = self.add_variable(DeltaG, rxn, lb=DGR_lb, ub=DGR_ub)

            # add the delta G naught as a variable
            RxnDGerror = rxn.thermo['deltaGRerr']
            DGoR= self.add_variable(DeltaGstd,
                                    rxn,
                                    lb = rxn.thermo['deltaGR'] - RxnDGerror,
                                    ub = rxn.thermo['deltaGR'] + RxnDGerror)


            # Initialization of indices and coefficients for all possible
            # scenaria:
            LC_TransMet = 0
            LC_ChemMet = 0
            P_expr = 0

            if rxn.thermo['isTrans']:
                # calculate the DG component associated to transport of the
                # metabolite. This will be added to the constraint on the Right
                # Hand Side (RHS)

                transportedMets = find_transported_mets(rxn)

                # Chemical coefficient, it is the metabolite's coefficient...
                # + transport coeff for reactants
                # - transport coeff for products
                chem_stoich = rxn.metabolites.copy()

                # Adding the terms for the transport part
                for seed_id, trans in transportedMets.items():
                    for type in ['reactant', 'product']:
                        if trans[type].formula != 'H':
                            LC_TransMet += (self.LC_vars[trans[type]]
                                            * RT
                                            * trans['coeff']
                                            * (
                                                -1 if type == 'reactant' else 1))

                        chem_stoich[trans[type]] += (trans['coeff']
                                                     * (
                                                         1 if type == "reactant" else -1))

                # Also add the chemical reaction part
                chem_stoich = {met: val for met, val in chem_stoich.items()
                               if val != 0}

                for met in chem_stoich:
                    metFormula = met.formula
                    if metFormula not in ['H', 'H2O']:
                        LC_ChemMet += self.LC_vars[met] * RT * chem_stoich[met]

            else:
                # if it is just a regular chemical reaction
                if add_potentials:
                    RHS_DG = 0

                    for met in rxn.metabolites:
                        metformula = met.formula
                        metDeltaGFtr = met.thermo.deltaGf_tr
                        if metformula == 'H2O':
                            RHS_DG = (RHS_DG
                                      + rxn.metabolites[met] * metDeltaGFtr)
                        elif metformula != 'H':
                            P_expr += self.P_vars[met] * rxn.metabolites[met]
                else:
                    # RxnDGnaught on the right hand side
                    RHS_DG = rxn.thermo['deltaGR']

                    for met in rxn.metabolites:
                        metformula = met.formula
                        if metformula not in ['H', 'H2O']:
                            # we use the LC here as we already accounted for the
                            # changes in deltaGFs in the RHS term
                            LC_ChemMet += (self.LC_vars[met]
                                           * RT
                                           * rxn.metabolites[met])

            # G: - DGR_rxn + DGoRerr_Rxn
            #   + RT * StoichCoefProd1 * LC_prod1
            #   + RT * StoichCoefProd2 * LC_prod2
            #   + RT * StoichCoefSub1 * LC_subs1
            #   + RT * StoichCoefSub2 * LC_subs2
            #   - ...
            #   = 0


            # Formulate the constraint
            CLHS = DGoR - DGR + LC_TransMet + LC_ChemMet
            self.add_constraint( NegativeDeltaG, rxn, CLHS, lb=0, ub=0)

            if add_displacement:
                lngamma = self.add_variable(ThermoDisplacement,
                                            rxn,
                                            lb=-BIGM_P,
                                            ub=BIGM_P)
                # ln(Gamma) = +DGR/RT (DGR < 0 , rxn is forward, ln(Gamma) < 0d
                expr = lngamma - 1/RT * DGR
                self.add_constraint(DisplacementCoupling,
                                    rxn,
                                    expr,
                                    lb=0,
                                    ub=0)


            # Create the use variables constraints and connect them to the
            # deltaG if the reaction has thermo constraints
            # FU_rxn: 1000 FU_rxn + DGR_rxn < 1000 - epsilon
            FU_rxn = self.add_variable(ForwardUseVariable, rxn)
            CLHS = DGR + FU_rxn * BIGM_THERMO
            self.add_constraint(ForwardDeltaGCoupling,
                                rxn,
                                CLHS,
                                ub=BIGM_THERMO - epsilon)

            # BU_rxn: 1000 BU_rxn - DGR_rxn < 1000 - epsilon
            BU_rxn = self.add_variable(BackwardUseVariable, rxn)

            CLHS = BU_rxn * BIGM_THERMO - DGR
            self.add_constraint(BackwardDeltaGCoupling,
                                rxn,
                                CLHS,
                                ub=BIGM_THERMO - epsilon)


        else:
            if not NotDrain:
                self.logger.debug('generating only use constraints for drain reaction'
                      + rxn.id)
            else:
                self.logger.debug(
                    'generating only use constraints for reaction' + rxn.id)

            FU_rxn = self.add_variable(ForwardUseVariable, rxn)
            BU_rxn = self.add_variable(BackwardUseVariable, rxn)

        # create the prevent simultaneous use constraints
        # SU_rxn: FU_rxn + BU_rxn <= 1
        CLHS = FU_rxn + BU_rxn
        # self.add_constraint(SimultaneousUse, rxn, CLHS, ub=1)
        # TEST: equality
        self.add_constraint(SimultaneousUse, rxn, CLHS, lb= 0, ub=1)

        # create constraints that control fluxes with their use variables
        # UF_rxn: F_rxn - M FU_rxn < 0
        F_rxn = rxn.forward_variable
        CLHS = F_rxn - FU_rxn * BIGM
        self.add_constraint(ForwardDirectionCoupling, rxn, CLHS, ub=0)

        # UR_rxn: R_rxn - M RU_rxn < 0
        R_rxn = rxn.reverse_variable
        CLHS = R_rxn - BU_rxn * BIGM
        self.add_constraint(BackwardDirectionCoupling, rxn, CLHS, ub=0)

    def convert(self,
                add_potentials=False,
                add_displacement=False,
                verbose=True):
        """ Converts a model into a tFBA ready model by adding the
        thermodynamic constraints required

        .. warning::
            This function requires you to have already called
            :func:`~.pytfa.ThermoModel.prepare`, otherwise it will raise an Exception !

        """

        self.logger.info('# Model conversion starting...')

        ###########################################
        # CONSTANTS & PARAMETERS for tFBA problem #
        ###########################################

        # value for the bigM in big M constraints such as:
        # UF_rxn: F_rxn - M*FU_rxn < 0
        bigM = BIGM
        # Check each reactions' bounds
        for reaction in self.reactions:
            if reaction.lower_bound < -bigM or reaction.upper_bound > bigM:
                raise Exception('flux bounds too wide or big M not big enough')


        ###################
        # INPUTS & CHECKS #
        ###################

        # check if model reactions has been checked if they are transport reactions
        try:
            for reaction in self.reactions:
                if not 'isTrans' in reaction.thermo:
                    reaction.thermo['isTrans'] = check_transport_reaction(
                        reaction)
        except:
            raise Exception('Reaction thermo data missing. '
                            + 'Please run ThermoModel.prepare()')

        # FIXME Use generalized rule (ext to the function)
        # formatting the metabolite and reaction names to remove brackets
        replacements = {
            '_': re.compile(r'[\[\(]'),
            '': re.compile(r'[\]\)]')
        }
        for items in [self.metabolites, self.reactions]:
            for item in items:
                for rep in replacements:
                    item.name = re.sub(replacements[rep], rep, item.name)

        self.LC_vars = {}
        self.P_vars = {}

        for met in self.metabolites:
            self._convert_metabolite(met, add_potentials, verbose)

        ## For each reaction...
        for rxn in self.reactions:
            self._convert_reaction(rxn, add_potentials,
                                        add_displacement, verbose)

        # CONSISTENCY CHECKS

        # Creating the objective
        if len(self.objective.variables) == 0:
            self.logger.warning('Objective not found')

        self.logger.info('# Model conversion done.')
        self.logger.info('# Updating model variables...')
        self.repair()
        self.logger.info('# model variables are up-to-date')

    def add_variable(self, kind, hook, **kwargs):
        """ Add a new variable to a COBRApy model.

        :param cobra.core.model.Model model: The COBRApy model
        :param string,cobra.Reaction hook: Either a string representing the name
            of the variable to add to the model, or a reaction object if the
            kind allows it

        :returns: The created variable
        :rtype: optlang.interface.Variable

        """


        # Initialisation links to the model
        var = kind(hook,
                # lb=lower_bound if lower_bound != float('-inf') else None,
                # ub=upper_bound if upper_bound != float('inf') else None,
                **kwargs)

        self._var_dict[var.name] = var
        #self.add_cons_vars(var.variable)

        return var

    def add_constraint(self, kind, hook, expr, **kwargs):
        """ Add a new constraint to a COBRApy model

        :param cobra.core.model.Model model: The COBRApy model
        :param string,cobra.Reaction hook: Either a string representing the name
            of the variable to add to the model, or a reaction object if the
            kind allows it
        :param sympy.core.expr.Expr expr: The expression of the constraint

        :returns: The created constraint
        :rtype: optlang.interface.Constraint

        """

        if isinstance(expr,GenericVariable):
            # make sure we actually pass the optlang variable
            expr = expr.variable

        # Initialisation links to the model
        cons = kind(hook,
                    expr,
                    # problem = self.problem,
                    # lb=lower_bound if lower_bound != float('-inf') else None,
                    # ub=upper_bound if upper_bound != float('inf') else None,
                    **kwargs)
        self._cons_dict[cons.name] = cons
        # self.add_cons_vars(cons.constraint)

        return cons

    def remove_variable(self, var):
        """
        Removes a variable

        :param var:
        :return:
        """

        self._var_dict.pop(var.name)
        self.remove_cons_vars(var.variable)

    def remove_constraint(self, cons):
        """
        Removes a constraint

        :param cons:
        :return:
        """

        self._cons_dict.pop(cons.name)
        self.remove_cons_vars(cons.constraint)

    def regenerate_variables(self):
        """
        Generates references to the model's constraints in self._cons_dict
        as tab-searchable attributes of the thermo model
        :return:
        """

        # Let us not forget to remove fields that migh be empty by now
        if hasattr(self,'_var_kinds'):
            for k in self._var_kinds:
                attrname = camel2underscores(k)
                delattr(self, attrname)

        _var_kinds = defaultdict(DictList)
        for k,v in self._var_dict.items():
            _var_kinds[v.__class__.__name__].append(v)

        for k in _var_kinds:
            attrname = camel2underscores(k)
            setattr(self,attrname,_var_kinds[k])

        self._var_kinds = _var_kinds


    def regenerate_constraints(self):
        """
        Generates references to the model's constraints in self._cons_dict
        as tab-searchable attributes of the thermo model
        :return:
        """

        # Let us not forget to remove fields that migh be empty by now
        if hasattr(self,'_cons_kinds'):
            for k in self._cons_kinds:
                attrname = camel2underscores(k)
                delattr(self, attrname)

        _cons_kinds = defaultdict(DictList)

        for k,v in self._cons_dict.items():
            _cons_kinds[v.__class__.__name__].append(v)

        for k in _cons_kinds:
            attrname = camel2underscores(k)
            setattr(self,attrname,_cons_kinds[k])

        self._cons_kinds = _cons_kinds


    def repair(self):
        """
        Updates references to variables and constraints
        :return:
        """
        # self.add_cons_vars([x.constraint for x in self._cons_dict.values()])
        # self.add_cons_vars([x.variable for x in self._var_dict.values()])
        Model.repair(self)
        self.regenerate_constraints()
        self.regenerate_variables()

    def get_primal(self, vartype, index_by_reactions = False):
        """
        Returns the primal value of the model for variables of a given type

        :param vartype: Class of variable. Ex: pytfa.optim.variables.ThermoDisplacement
        :return:
        """
        return get_primal(self, vartype, index_by_reactions)

    def get_solution(self):
        """
        Overrides the cobra.core.solution method, to also get the supplementary
        variables we added to the model
        :return:
        """
        objective_value = self.solver.objective.value
        status = self.solver.status
        variables = pd.Series(data=self.solver.primal_values)
        solution = Solution(objective_value=objective_value,
                            status=status,
                            fluxes=variables)
        return solution

    def optimize(self, objective_sense=None, **kwargs):
        """
        Call the Model.optimize function (which really is but an interface to the
        solver's. Catches SolverError in the case of no solutions. Passes down
        supplementary keyword arguments (see cobra.core.Model.optimize)
        :type objective_sense: 'min' or 'max'
        """

        if objective_sense:
            self.objective.direction = objective_sense

        try:
            Model.optimize(self, **kwargs)
            solution = self.get_solution()
            self.solution = solution
            return solution
        except SolverError as SE:
            status = self.solver.status
            self.logger.error(SE)
            self.logger.warning('Solver status: {}'.format(status))
            raise(SE)

    def get_constraints_of_type(self, constraint_type):
        """
        Convenience function that takes as input a constraint class and returns
        all its instances within the model

        :param constraint_type:
        :return:
        """

        constraint_key = constraint_type.__name__
        return self._cons_kinds[constraint_key]

    def get_variables_of_type(self, variable_type):
        """
        Convenience function that takes as input a variable class and returns
        all its instances within the model

        :param variable_type:
        :return:
        """

        variable_key = variable_type.__name__
        return self._var_kinds[variable_key]

    def print_info(self):
        """
        Print information and counts for the model
        :return:
        """
        n_metabolites   = len(self.metabolites)
        n_reactions     = len(self.reactions)
        n_metabolites_thermo = len([x for x in self.metabolites \
                                    if x.thermo['id']])
        n_reactions_thermo   = len([x for x in self.reactions   \
                                    if x.thermo['computed']])

        info = pd.DataFrame(columns = ['value'])
        info.loc['name'] = self.name
        info.loc['description'] = self.description
        info.loc['num metabolites'] = n_metabolites
        info.loc['num reactions'] = n_reactions
        info.loc['num metabolite(thermo)'] = n_metabolites_thermo
        info.loc['num reactions(thermo)'] = n_reactions_thermo
        info.loc['pct metabolite(thermo)'] = n_metabolites_thermo/n_metabolites*100
        info.loc['pct reactions(thermo)'] = n_reactions_thermo/n_reactions*100
        info.index.name = 'key'

        print(info)

    def __deepcopy__(self,memo):
        """

        :param memo:
        :return:
        """
        # FIXME

        return self.copy()

    def copy(self):
        new = type(self)(self.thermo_data,
                          self.parent,
                          name=self.name,
                          temperature = self.TEMPERATURE,
                          min_ph = self.MIN_pH,
                          max_ph = self.MAX_pH)

        new.name = 'Copy of ' + self.name

        # The solver has all the properties we need. From there, we can
        # reconstruct the model
        new_solver = self.solver.clone(self.solver)
        new._solver = new_solver

        for this_var in self._var_dict.values():
            new_solver_var = new.variables.get(this_var.name)

            if isinstance(this_var, ReactionVariable):
                reaction = new.reactions.get_by_id(this_var.reaction.id)
                nv = new.add_variable(kind = this_var.__class__,
                                      hook = reaction)

            elif isinstance(this_var, MetaboliteVariable):
                metabolite = new.metabolites.get_by_id(this_var.metabolite.id)
                nv = new.add_variable(kind = this_var.__class__,
                                      hook = metabolite)

            else:
                raise TypeError('Class {} copy not handled yet'    \
                                .format(this_var.__class__))

        for this_cons in self._cons_dict.values():
            new_solver_cons = new.constraints.get(this_cons.name)

            expr = new_solver_cons.expression
            if isinstance(this_cons, ReactionConstraint):
                reaction = new.reactions.get_by_id(this_cons.reaction.id)
                nc = new.add_constraint(kind = this_cons.__class__,
                                        hook = reaction,
                                        expr = expr)
                nc.constraint.lb = this_cons.constraint.lb
                nc.constraint.ub = this_cons.constraint.ub
            elif isinstance(this_cons, MetaboliteConstraint):
                metabolite = new.metabolites.get_by_id(this_cons.metabolite.id)
                nc = new.add_constraint(kind = this_cons.__class__,
                                        hook = metabolite,
                                        expr = expr)
                nc.constraint.lb = this_cons.constraint.lb
                nc.constraint.ub = this_cons.constraint.ub
            else:
                raise TypeError('Class {} copy not handled yet'    \
                                .format(this_cons.__class__))

        new.repair()
        return new