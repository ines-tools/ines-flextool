import spinedb_api as api
from spinedb_api import DatabaseMapping
from ines_tools import ines_transform
from spinedb_api.exception import NothingToCommit
from sqlalchemy.exc import DBAPIError
import sys
import yaml
import numpy as np
from collections import defaultdict
from dateutil.relativedelta import relativedelta
from spinedb_api.exception import NothingToCommit

#if len(sys.argv) > 1:
#    url_db_in = sys.argv[1]
#else:
#    exit("Please provide input database url and output database url as arguments. They should be of the form ""sqlite:///path/db_file.sqlite""")
#if len(sys.argv) > 2:
#    url_db_out = sys.argv[2]
#else:
#    exit("Please provide input database url and output database url as arguments. They should be of the form ""sqlite:///path/db_file.sqlite""")

url_db_in = "sqlite:///C:/Users/aetart/Documents/ines-flextool/ines-spec.sqlite"
url_db_out = "sqlite:///C:/Users/aetart/Documents/ines-flextool/input_data_template.sqlite"


with open('ines_to_flextool_entities.yaml', 'r') as file:
    entities_to_copy = yaml.load(file, yaml.BaseLoader)
with open('ines_to_flextool_parameters.yaml', 'r') as file:
    parameter_transforms = yaml.load(file, yaml.BaseLoader)
with open('ines_to_flextool_methods.yaml', 'r') as file:
    parameter_methods = yaml.load(file, yaml.BaseLoader)
with open('ines_to_flextool_entities_to_parameters.yaml', 'r') as file:
    entities_to_parameters = yaml.load(file, yaml.BaseLoader)


def main():
    with DatabaseMapping(url_db_in) as source_db:
        with DatabaseMapping(url_db_out) as target_db:
            ## Empty the database
            target_db.purge_items('parameter_value')
            target_db.purge_items('entity')
            target_db.purge_items('alternative')
            target_db.refresh_session()
            target_db.commit_session("Purged stuff")
            ## Copy alternatives
            for alternative in source_db.get_alternative_items():
                target_db.add_alternative_item(name=alternative["name"])
            for scenario in source_db.get_scenario_items():
                target_db.add_scenario_item(name=scenario["name"])
            for scenario_alternative in source_db.get_scenario_alternative_items():
                target_db.add_scenario_alternative_item(alternative_name=scenario_alternative["alternative_name"],
                                                        scenario_name=scenario_alternative["scenario_name"],
                                                        rank=scenario_alternative["rank"])
            try:
                target_db.commit_session("Added scenarios and alternatives")
            except NothingToCommit:
                print("No scenarios or alternatives to commit")
            except DBAPIError as e:
                print(e)

            ## Copy entites
            print("copy_entities")
            target_db = ines_transform.copy_entities(source_db, target_db, entities_to_copy)
            ## Copy numeric parameters(source_db, target_db, copy_entities)
            print("transfrom_parameters")
            target_db = ines_transform.transform_parameters(source_db, target_db, parameter_transforms, ts_to_map=True)
            ## Copy method parameters
            print("process methods")
            target_db = ines_transform.process_methods(source_db, target_db, parameter_methods)
            ## Copy entities to parameters
            #target_db = ines_transform.copy_entities_to_parameters(source_db, target_db, entities_to_parameters)
            ## Copy capacity specific parameters (manual scripting)
            print("process capacities")
            target_db = process_capacities(source_db, target_db)
            print("process efficiencies")
            target_db = process_efficiency(source_db, target_db)
            print("process profiles")
            target_db = create_profiles(source_db, target_db)
            ## Create user constraint for unit_flow__unit_flow
            print("process user constraints")
            target_db = process_user_constraints(source_db, target_db)
            ## Create a timeline from start_time and duration
            print("create timelines")
            target_db = create_timeline(source_db, target_db)


def create_profiles(source_db, target_db):

    classes = ['unit__to_node','node__to_unit']
    for entity_class in classes:
        profile_limit_upper = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_upper")
        profile_limit_upper_forecasts = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_upper_forecasts")
        profile_limit_lower = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_lower")
        profile_limit_lower_forecasts = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_lower_forecasts")
        profile_limit_fix = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_fix")
        profile_limit_fix_forecasts = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_limit_fix_forecasts")
        profile_methods = source_db.get_parameter_value_items(entity_class_name=entity_class, parameter_definition_name="profile_method")
        
        for source_entity in source_db.get_entity_items(entity_class_name=entity_class):
            added_entities = [] #just for the possibility of a profile_method without values
            if entity_class == 'unit__to_node':
                node = source_entity["entity_byname"][1]
                unit = source_entity["entity_byname"][0]
            else:
                node = source_entity["entity_byname"][0]
                unit = source_entity["entity_byname"][1]

            if (any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_upper) or 
                any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_upper_forecasts)):
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name="profile",
                                                                            entity_byname=(unit+"_upper",)), warn=True)
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit__node__profile',
                                                                            entity_byname=(unit,node,unit+"_upper")), warn=True)
                added_entities.append((unit,node,unit+"_upper"))
                
            if (any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_lower) or 
                any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_lower_forecasts)):
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name="profile",
                                                                            entity_byname=(unit+"_lower",)), warn=True)
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit__node__profile',
                                                                            entity_byname=(unit,node,unit+"_lower")), warn=True)
                added_entities.append((unit,node,unit+"_lower"))
            
            if (any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_fix) or 
                any(param["entity_byname"] == source_entity["entity_byname"] for param in profile_limit_fix_forecasts)):
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name="profile",
                                                                            entity_byname=(unit+"_fix",)), warn=True)
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit__node__profile',
                                                                            entity_byname=(unit,node,unit+"_fix")), warn=True)
                added_entities.append((unit,node,unit+"_fix"))
            for param in profile_limit_upper:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_upper",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
            for param in profile_limit_upper_forecasts:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_upper",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
            for param in profile_limit_lower:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_lower",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
            for param in profile_limit_lower_forecasts:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_lower",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
            for param in profile_limit_fix:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_fix",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
            for param in profile_limit_fix_forecasts:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt_ent_class_target = [param["alternative_name"], (unit+"_fix",), "profile"]
                    target_db = pass_profile_value(target_db, param, alt_ent_class_target)
    
            for param in profile_methods:
                if param["entity_byname"] == source_entity["entity_byname"]:
                    alt = param["alternative_name"]
                    profile_method = api.from_database(param["value"], param["type"])
                    if profile_method != 'no_profile':
                        if profile_method=='upper_and_lower_limit':
                            if (unit,node,unit+"_upper") in added_entities:
                                target_db = ines_transform.add_item_to_DB(target_db, "profile_method", 
                                                                            [alt, (unit,node,unit+"_upper"), "unit__node__profile"], "upper_limit")
                            if (unit,node,unit+"_lower") in added_entities:
                                target_db = ines_transform.add_item_to_DB(target_db, "profile_method", 
                                                                            [alt, (unit,node,unit+"_lower"), "unit__node__profile"], "lower_limit")
                        elif profile_method=='upper_limit':
                            if (unit,node,unit+"_upper") in added_entities:
                                target_db = ines_transform.add_item_to_DB(target_db, "profile_method", [alt, (unit,node,unit+"_upper"), "unit__node__profile"], profile_method)
                        elif profile_method=='lower_limit':
                            if (unit,node,unit+"_lower") in added_entities:
                                target_db = ines_transform.add_item_to_DB(target_db, "profile_method", [alt, (unit,node,unit+"_lower"), "unit__node__profile"], profile_method)
                        elif profile_method=='fixed':
                            if (unit,node,unit+"_fix") in added_entities:
                                target_db = ines_transform.add_item_to_DB(target_db, "profile_method", [alt, (unit,node,unit+"_fix"), "unit__node__profile"], profile_method)


    try:
        target_db.commit_session("Added profile entities and values")
    except NothingToCommit:
        print("No profile constraints to commit")
    except DBAPIError as e:
        print("commit profile parameters error")
    return target_db

def pass_profile_value(target_db, param, alt_ent_class_target):
    profile = api.from_database(param["value"], param["type"])
    if isinstance(profile,api.parameter_value.Map):
        if isinstance(profile.values[0], api.parameter_value.Map):
            forecasts = list()
            inner_list = list()
            for i, ind in enumerate(profile.indexes):
                values_map = profile.values[i]
                inner_list.append(api.Map([str(x) for x in values_map.indexes], [float(x) for x in values_map.values]))
                forecasts.append(str(ind))
            value = api.Map(forecasts, inner_list)
        else:
            value = api.Map([str(x) for x in profile.indexes], [float(x) for x in profile.values])
        target_db = ines_transform.add_item_to_DB(target_db, "profile", alt_ent_class_target, value, value_type='map')
    return target_db

def process_capacities(source_db, target_db):
    unit__to_node_entities = source_db.get_entity_items(entity_class_name="unit__to_node")
    unit__unit_to_node = defaultdict(list)
    for u_to_n in unit__to_node_entities:
        unit__unit_to_node[u_to_n["entity_byname"][0]].append([u_to_n["entity_byname"]])
    node__to_unit_entities = source_db.get_entity_items(entity_class_name="node__to_unit")
    unit__node_to_unit = defaultdict(list)
    for n_to_u in node__to_unit_entities:
        unit__node_to_unit[n_to_u["entity_byname"][1]].append([n_to_u["entity_byname"]])

    units_max_cumulatives = source_db.get_parameter_value_items(entity_class_name="unit", parameter_definition_name="units_max_cumulative")
    units_min_cumulatives = source_db.get_parameter_value_items(entity_class_name="unit", parameter_definition_name="units_min_cumulative")
    units_existing_source = source_db.get_parameter_value_items(entity_class_name="unit", parameter_definition_name="units_existing")
    un_capacity = source_db.get_parameter_value_items(entity_class_name="unit__to_node", parameter_definition_name="capacity")
    un_investment_cost = source_db.get_parameter_value_items(entity_class_name="unit__to_node", parameter_definition_name="investment_cost")
    un_fixed_cost = source_db.get_parameter_value_items(entity_class_name="unit__to_node", parameter_definition_name="fixed_cost")
    un_salvage_value = source_db.get_parameter_value_items(entity_class_name="unit__to_node", parameter_definition_name="salvage_value")
    nu_capacity = source_db.get_parameter_value_items(entity_class_name="node__to_unit", parameter_definition_name="capacity")
    nu_investment_cost = source_db.get_parameter_value_items(entity_class_name="node__to_unit", parameter_definition_name="investment_cost")
    nu_fixed_cost = source_db.get_parameter_value_items(entity_class_name="node__to_unit", parameter_definition_name="fixed_cost")
    nu_salvage_value = source_db.get_parameter_value_items(entity_class_name="node__to_unit", parameter_definition_name="salvage_value")
 
    for unit_source in source_db.get_entity_items(entity_class_name="unit"):
        units_existing = {}
        u_to_n_capacity = {}
        u_to_n_investment_cost = {}
        u_to_n_fixed_cost = {}
        u_to_n_salvage_value = {}
        n_to_u_capacity = {}
        n_to_u_investment_cost = {}
        n_to_u_fixed_cost = {}
        n_to_u_salvage_value = {}

        # Store parameter units_existing (for the alternatives that define it)
        for param in units_existing_source:
            if unit_source["name"] == param["entity_name"]:
                value = api.from_database(param["value"], param["type"])
                if value:
                    units_existing[param["alternative_name"]] = value
                    alt_ent_class = (param["alternative_name"], unit_source["entity_byname"], "unit")
                    # Write 'number of existing units' to the db (assuming ines_db uses number of units as it should)
                    target_db = ines_transform.add_item_to_DB(target_db, "existing", alt_ent_class, value)
        # Store capacity related parameters in unit__to_node_source (for the alternatives that define it)
        for unit__to_node_sources in unit__unit_to_node[unit_source["name"]]:
            for unit__to_node_source in unit__to_node_sources:
                params = [param for param in un_capacity if param["entity_byname"] == unit__to_node_source]
                u_to_n_capacity = params_to_dict(u_to_n_capacity, params)
                params = [param for param in un_investment_cost if param["entity_byname"] == unit__to_node_source]
                u_to_n_investment_cost = params_to_dict(u_to_n_investment_cost, params)
                params = [param for param in un_fixed_cost if param["entity_byname"] == unit__to_node_source]
                u_to_n_fixed_cost = params_to_dict(u_to_n_fixed_cost, params)
                params = [param for param in un_salvage_value if param["entity_byname"] == unit__to_node_source]
                u_to_n_salvage_value = params_to_dict(u_to_n_salvage_value, params)

        # If outputs don't have capacity defined, start plan B: Store parameter capacity in node__to_unit_source (for the alternatives that define it)
        if not u_to_n_capacity:
            for node__to_unit_sources in unit__node_to_unit[unit_source["name"]]:
                for node__to_unit_source in node__to_unit_sources:
                    params = [param for param in nu_capacity if param["entity_byname"] == node__to_unit_source]
                    n_to_u_capacity = params_to_dict(n_to_u_capacity, params)

        # If outputs don't have investment_cost defined, start plan B: Store parameters investment_cost, fixed_cost and salvage_value in node__to_unit_source (for the alternatives that define it)
        if not u_to_n_investment_cost:
            for node__to_unit_sources in unit__node_to_unit[unit_source["name"]]:
                for node__to_unit_source in node__to_unit_sources:
                    params = [param for param in nu_investment_cost if param["entity_byname"] == node__to_unit_source]
                    n_to_u_investment_cost = params_to_dict(n_to_u_investment_cost, params)
                    params = [param for param in nu_fixed_cost if param["entity_byname"] == node__to_unit_source]
                    n_to_u_fixed_cost = params_to_dict(n_to_u_fixed_cost, params)
                    params = [param for param in nu_salvage_value if param["entity_byname"] == node__to_unit_source]
                    n_to_u_salvage_value = params_to_dict(n_to_u_salvage_value, params)

        # Write virtual_unitsize to FlexTool DB (if capacity defined in unit outputs)
        if u_to_n_capacity:
            for u_to_n_alt, u_to_n_val in u_to_n_capacity.items():
                alt_ent_class = (u_to_n_alt, unit_source["entity_byname"], "unit")
                if not isinstance(u_to_n_val, float):
                    exit("Unit_to_node capacity in ines_db needs to be a constant float")
                target_db = ines_transform.add_item_to_DB(target_db, "virtual_unitsize", alt_ent_class, u_to_n_val)
                for units_max_cumulative in units_max_cumulatives:
                    if unit_source["name"] == units_max_cumulative["entity_name"]:
                        if isinstance(units_max_cumulative["parsed_value"],list):
                            cul_capacity_list = []
                            for value in units_max_cumulative["parsed_value"].values:
                                cul_capacity_list.append(round(value * u_to_n_val, 6))
                            cul_capacity_map = api.Map(indexes=units_max_cumulative["parsed_value"].indexes, values=cul_capacity_list, index_name="period")
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_max_capacity", alt_ent_class, cul_capacity_map)
                        else:
                            value = round(units_max_cumulative["parsed_value"]* u_to_n_val, 6)
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_max_capacity", alt_ent_class, value)
                for units_min_cumulative in units_min_cumulatives:
                    if unit_source["name"] == units_min_cumulative["entity_name"]:
                        if isinstance(units_min_cumulative["parsed_value"],list):
                            cul_capacity_list = []
                            for value in units_min_cumulative["parsed_value"].values:
                                cul_capacity_list.append(round(value * u_to_n_val, 6))
                            cul_capacity_map = api.Map(indexes=units_min_cumulative["parsed_value"].indexes, values=cul_capacity_list, index_name="period")
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_min_capacity", alt_ent_class, cul_capacity_map)
                        else:
                            value = round(units_min_cumulative["parsed_value"]* u_to_n_val, 6)
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_min_capacity", alt_ent_class, value)
        # Write virtual_unitsize to FlexTool DB (if capacity is defined in unit inputs instead)
        elif n_to_u_capacity:
            for n_to_u_alt, n_to_u_val in n_to_u_capacity.items():
                alt_ent_class = (n_to_u_alt, unit_source["entity_byname"], "unit")
                if not isinstance(n_to_u_val, float):
                    exit("Node_to_unit capacity in ines_db needs to be a constant float")
                target_db = ines_transform.add_item_to_DB(target_db, "virtual_unitsize", alt_ent_class, n_to_u_val)
                for units_max_cumulative in units_max_cumulatives:
                    if unit_source["name"] == units_max_cumulative["entity_name"]:
                        if isinstance(units_max_cumulative["parsed_value"],list):
                            cul_capacity_list = []
                            for value in units_max_cumulative["parsed_value"].values:
                                cul_capacity_list.append(round(value * n_to_u_val, 6))
                            cul_capacity_map = api.Map(indexes=units_max_cumulative["parsed_value"].indexes, values=cul_capacity_list, index_name="period")
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_max_capacity", alt_ent_class, cul_capacity_map)
                        else: 
                            value = round(units_max_cumulative["parsed_value"]* n_to_u_val, 6)
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_max_capacity", alt_ent_class, value)
                for units_min_cumulative in units_min_cumulatives:
                    if unit_source["name"] == units_min_cumulative["entity_name"]:
                        if isinstance(units_min_cumulative["parsed_value"],list):
                            cul_capacity_list = []
                            for value in units_min_cumulative["parsed_value"].values:
                                cul_capacity_list.append(round(value * n_to_u_val, 6))
                            cul_capacity_map = api.Map(indexes=units_min_cumulative["parsed_value"].indexes, values=cul_capacity_list, index_name="period")
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_min_capacity", alt_ent_class, cul_capacity_map)
                        else: 
                            value = round(units_min_cumulative["parsed_value"]* n_to_u_val, 6)
                            target_db = ines_transform.add_item_to_DB(target_db, "cumulative_min_capacity", alt_ent_class, value)
        # Write 'investment_cost', 'fixed_cost' and 'salvage_value' to FlexTool DB (if investment_cost defined in unit outputs)
        if u_to_n_investment_cost:
            for u_to_n_alt, u_to_n_val in u_to_n_investment_cost.items():
                alt_ent_class = (u_to_n_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "invest_cost", alt_ent_class, u_to_n_val * 0.001)
            for u_to_n_alt, u_to_n_val in u_to_n_fixed_cost.items():
                alt_ent_class = (u_to_n_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "fixed_cost", alt_ent_class, u_to_n_val)
            for u_to_n_alt, u_to_n_val in u_to_n_salvage_value.items():
                alt_ent_class = (u_to_n_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "salvage_value", alt_ent_class, u_to_n_val)
        # Write 'investment_cost', 'fixed_cost' and 'salvage_value' to FlexTool DB (if investment_cost is defined in unit inputs instead)
        elif n_to_u_investment_cost:
            for n_to_u_alt, n_to_u_val in n_to_u_investment_cost.items():
                alt_ent_class = (n_to_u_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "invest_cost", alt_ent_class, n_to_u_val * 0.001)
            for n_to_u_alt, n_to_u_val in n_to_u_fixed_cost.items():
                alt_ent_class = (n_to_u_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "fixed_cost", alt_ent_class, n_to_u_val)
            for n_to_u_alt, n_to_u_val in n_to_u_salvage_value.items():
                alt_ent_class = (n_to_u_alt, unit_source["entity_byname"], "unit")
                target_db = ines_transform.add_item_to_DB(target_db, "salvage_value", alt_ent_class, n_to_u_val)

        # If no capacity nor investment_cost defined, warn.
        if not (u_to_n_capacity or u_to_n_investment_cost or n_to_u_capacity or n_to_u_investment_cost):
            print("Unit without capacity or investment_cost:" + unit_source["name"])

    try:
        target_db.commit_session("Added process capacities")
    except DBAPIError as e:
        print("commit process capacities error")
    return target_db

def process_efficiency(source_db, target_db):

    efficiencies = source_db.get_parameter_value_items(entity_class_name="unit", parameter_definition_name="efficiency")
    for param in efficiencies:
        in_val = api.from_database(param["value"], param["type"])
        if isinstance(in_val, api.Map):
            if len(in_val.indexes) == 2: #minload eff
                target_db = ines_transform.add_item_to_DB(target_db, "min_load", [param["alternative_name"], param["entity_byname"], "unit"], float(in_val.indexes[0]))
                target_db = ines_transform.add_item_to_DB(target_db, "efficiency_at_min_load", [param["alternative_name"], param["entity_byname"], "unit"], float(in_val.values[0]))
                target_db = ines_transform.add_item_to_DB(target_db, "efficiency", [param["alternative_name"], param["entity_byname"], "unit"], float(in_val.values[1]))
            else: #piecewise, cannot convert
                print(f'piecewise eff cannot be converted for: {param["entity_byname"]}')
        elif isinstance(in_val, float):
            target_db = ines_transform.add_item_to_DB(target_db, "efficiency", [param["alternative_name"], param["entity_byname"], "unit"], in_val)
    return target_db


def process_user_constraints(source_db, target_db):
    alts = source_db.get_alternative_items()
    unit_flow__unit_flows = source_db.get_entity_items(entity_class_name="unit_flow__unit_flow")
    for alt in alts:
        for unit_flow__unit_flow in unit_flow__unit_flows:
            # For now handling just equality constraint (no greater than or less than)
            ines_ration_names = ["equality_ratio", "greater_than_ratio", "less_than_ratio"]
            ines_constant_names = ["equality_constant", "greater_than_constant", "less_than_constant"]
            sense_names = ["equal", "greater_than", "less_than"]  # Both use the same names
            for k, sense_name in enumerate(sense_names):
                constraint_name = unit_flow__unit_flow["name"] + "_" + sense_name
                ratio = source_db.get_parameter_value_item(entity_class_name="unit_flow__unit_flow",
                                                                    entity_byname=unit_flow__unit_flow["entity_byname"],
                                                                    parameter_definition_name=ines_ration_names[k],
                                                                    alternative_name=alt["name"])
                constant = source_db.get_parameter_value_item(entity_class_name="unit_flow__unit_flow",
                                                                       entity_byname=unit_flow__unit_flow["entity_byname"],
                                                                       parameter_definition_name=ines_constant_names[k],
                                                                       alternative_name=alt["name"])
                if ratio:
                    added, error = target_db.add_entity_item(entity_class_name="constraint",
                                                             entity_byname=(constraint_name, ))
                    if error:
                        print("Failed to add a constraint entity based on: " + constraint_name + " error: " + error)
                    added, error = target_db.add_entity_alternative_item(entity_class_name="constraint",
                                                                         entity_byname=(constraint_name, ),
                                                                         alternative_name=alt["name"])
                    if error:
                        print("Failed to add a constraint entity_alternative based on: " + constraint_name + " error: " + error)
                    p_value, p_type = api.to_database(sense_name)
                    added, error = target_db.add_parameter_value_item(entity_class_name="constraint",
                                                                      entity_byname=(constraint_name, ),
                                                                      parameter_definition_name="sense",
                                                                      alternative_name=alt["name"],
                                                                      type=p_type,
                                                                      value=p_value)
                    if error:
                        exit("Failed to add sense for a constraint of " + constraint_name)
                    if constant:
                        constant_parsed = constant["parsed_value"].values
                        if len(constant_parsed) > 1:
                            print("Constraint constant parameter contains a list, FlexTool handles only constants, using 1st value for: " + constraint_name)
                        p_value, p_type = api.to_database(constant_parsed[0])
                        added, error = target_db.add_parameter_value_item(entity_class_name="constraint",
                                                                          entity_byname=(constraint_name, ),
                                                                          parameter_definition_name="constant",
                                                                          alternative_name=alt["name"],
                                                                          type=p_type,
                                                                          value=p_value)
                        if error:
                            exit("Failed to add constant for constraint: " + constraint_name)
                    if isinstance(ratio["parsed_value"], float):
                        ratio_parsed = [ratio["parsed_value"]]
                    else:
                        ratio_parsed = ratio["parsed_value"].values
                        if len(ratio_parsed) > 1:
                            print("Constraint ratio parameter contains a list, FlexTool handles only constants, using 1st value for: " + constraint_name)
                    first_flow_coefficient = round(ratio_parsed[0] / (ratio_parsed[0] + 1), 6)
                    flow_coefficient_map = api.Map(indexes=[constraint_name], values=[first_flow_coefficient], index_name="constraint")
                    p_value, p_type = api.to_database(flow_coefficient_map)
                    flow_byname = None
                    entity_class_name = None
                    flow_byname_object = source_db.get_entity_item(entity_class_name="unit__to_node",
                                                                   entity_byname=(unit_flow__unit_flow["entity_byname"][0], unit_flow__unit_flow["entity_byname"][1]))
                    if flow_byname_object:
                        flow_byname = flow_byname_object["entity_byname"]
                        entity_class_name = "unit__outputNode"
                    else:
                        flow_byname_object = source_db.get_entity_item(entity_class_name="node__to_unit",
                                                                       entity_byname=(unit_flow__unit_flow["entity_byname"][0], unit_flow__unit_flow["entity_byname"][1]))
                        if flow_byname_object:
                            flow_byname = (flow_byname_object["entity_byname"][1], flow_byname_object["entity_byname"][0])
                            entity_class_name = "unit__inputNode"
                    if flow_byname:
                        added, error = target_db.add_parameter_value_item(entity_class_name=entity_class_name,
                                                                          entity_byname=flow_byname,
                                                                          parameter_definition_name="constraint_flow_coefficient",
                                                                          alternative_name=alt["name"],
                                                                          type=p_type,
                                                                          value=p_value)
                        if error:
                            exit("Failed to add constraint_flow_coefficient for " + str(flow_byname))
                        flow_byname_object = source_db.get_entity_item(entity_class_name="unit__to_node",
                                                                       entity_byname=(unit_flow__unit_flow["entity_byname"][2], unit_flow__unit_flow["entity_byname"][3]))
                        if flow_byname_object:
                            flow_byname = flow_byname_object["entity_byname"]
                            entity_class_name = "unit__outputNode"
                        else:
                            flow_byname_object = source_db.get_entity_item(entity_class_name="node__to_unit",
                                                                           entity_byname=(unit_flow__unit_flow["entity_byname"][2], unit_flow__unit_flow["entity_byname"][3]))
                            if flow_byname_object:
                                flow_byname = (flow_byname_object["entity_byname"][1], flow_byname_object["entity_byname"][0])
                                entity_class_name = "unit__inputNode"
                        if flow_byname:
                            second_flow_coefficient = -(1 - first_flow_coefficient)
                            flow_coefficient_map = api.Map(indexes=[constraint_name], values=[second_flow_coefficient], index_name="constraint")
                            p_value, p_type = api.to_database(flow_coefficient_map)
                            added, error = target_db.add_parameter_value_item(entity_class_name=entity_class_name,
                                                                              entity_byname=flow_byname,
                                                                              parameter_definition_name="constraint_flow_coefficient",
                                                                              alternative_name=alt["name"],
                                                                              type=p_type,
                                                                              value=p_value)
                            if error:
                                exit("Failed to add constraint_flow_coefficient for " + str(flow_byname) + ". " + error)
                        else:
                            print("Failed to find unit__to_node or node__to_unit for " + constraint_name)
                    else:
                        print("Failed to find unit__to_node or node__to_unit for " + constraint_name)
    try:
        target_db.commit_session("Added user constraints")
    except NothingToCommit:
        print("No user constraints to commit")
    except DBAPIError as e:
        print("Processing user constraints error")
    return target_db


def create_timeline(source_db, target_db):
    system_entity_names = []
    timeline_indexes = {}
    timeline_values = {}
    for system_entity in source_db.get_entity_items(entity_class_name="system"):
        system_entity_names.append(system_entity["name"])
        params = source_db.get_parameter_value_items(entity_class_name="system", entity_name=system_entity["name"], parameter_definition_name="timeline")
        if len(params) > 1:
            exit("Only one alternative value supported for the timeline parameter of one system entity.")
        for param in params:
            value = api.from_database(param["value"], param["type"])
            if value.VALUE_TYPE == 'time series':
                timeline_indexes[system_entity["name"]] = value.indexes
                timeline_values[system_entity["name"]] = value.values
                value = api.Map([str(x) for x in value.indexes], [float(x) for x in value.values],index_name=value.index_name)
            target_db = ines_transform.add_item_to_DB(target_db, "timestep_duration", [param["alternative_name"], (system_entity["name"], ), "timeline"], value)
    for solve_entity in source_db.get_entity_items(entity_class_name="solve_pattern"):
        period_list = []
        for param_period in source_db.get_parameter_value_items(solve_entity["entity_class_name"], entity_name=solve_entity["name"], parameter_definition_name="period"):
            solves_array = api.Array(values=[solve_entity["name"]], index_name="solves")
            p_value, p_type = api.to_database(solves_array)
            target_db.add_parameter_value_item(entity_class_name="model",
                                               parameter_definition_name="solves",
                                               entity_byname=(solve_entity["name"],),
                                               alternative_name=param_period["alternative_name"],
                                               value=p_value,
                                               type=p_type)
            period = api.from_database(param_period["value"], param_period["type"])
            period_list = period
            if param_period["type"] != "array":
                period_list = api.Array([period])
            target_db = ines_transform.add_item_to_DB(target_db, "realized_periods",
                                         [param_period["alternative_name"], (solve_entity["name"],), "solve"], period_list,
                                         value_type="array")
            target_db = ines_transform.add_item_to_DB(target_db, "invest_periods",
                                         [param_period["alternative_name"], (solve_entity["name"],), "solve"], period_list,
                                         value_type="array")
            if param_period["type"] == "array":
                timeblock_set_array = []
                for period_array_member in period.values:
                    timeblock_set_array.append(solve_entity["name"])
                period__timeblock_set = api.Map(period.values, timeblock_set_array, index_name="period")
            else:
                period__timeblock_set = api.Map([period], [solve_entity["name"]], index_name="period")
            target_db = ines_transform.add_item_to_DB(target_db, "period_timeblockSet", [param_period["alternative_name"], (solve_entity["name"],), "solve"], period__timeblock_set, value_type="map")
        
        start_time_params = source_db.get_parameter_value_items(entity_class_name=solve_entity["entity_class_name"],
                                                                entity_name=solve_entity["name"],
                                                                parameter_definition_name="start_time")
        duration_params = source_db.get_parameter_value_items(entity_class_name=solve_entity["entity_class_name"],
                                                            entity_name=solve_entity["name"],
                                                            parameter_definition_name="duration")
        start_time_alternatives = [start_time_params[a]["alternative_name"] for a in range(len(start_time_params))]
        duration_alternatives = [duration_params[a]["alternative_name"] for a in range(len(duration_params))]
        match_alternatives = set(start_time_alternatives) & set(duration_alternatives)
        for alt in match_alternatives:
            start_time_param = [start_time_params[a] for a in range(len(start_time_params)) if alt == start_time_params[a]["alternative_name"]]
            duration_param = [duration_params[a] for a in range(len(duration_params)) if alt == duration_params[a]["alternative_name"]]
            if start_time_param and duration_param:
                duration_value = api.from_database(duration_param[0]["value"], duration_param[0]["type"])
                start_time = api.from_database(start_time_param[0]["value"], start_time_param[0]["type"])
                if isinstance(duration_value, api.Duration):  #only one value
                    duration_value = list(duration_value)
                if isinstance(start_time, api.DateTime):
                    start_time = list(start_time)
                if len(duration_value) != len(start_time):
                    exit("duration and start_time parameters have different number of array elements under the same alternative (in same solve-pattern entity) - they need to match")
                durations = []
                for system_entity in system_entity_names:
                    timestep_counter = 0
                    block_start = None
                    block_iterator = iter(start_time.values)
                    start_t = next(block_iterator)
                    durations_counter = 0
                    for timeline_index in timeline_indexes[system_entity]:
                        if block_start and timeline_index > block_start.value + duration_value.values[durations_counter].value:
                            durations.append(timestep_counter)
                            durations_counter += 1
                            block_start = None
                            try:
                                start_t = next(block_iterator)
                            except StopIteration:
                                break
                        if block_start is None and np.datetime_as_string(timeline_index) == start_t.value.isoformat():
                            block_start = start_t
                            timestep_counter = 0
                        timestep_counter += 1
                    timeblocks_map = api.Map(indexes=[str(x) for x in start_time.values], values=durations, index_name="timestamp")
                    p_value, p_type = api.to_database(timeblocks_map)
                    added, error = target_db.add_parameter_value_item(entity_class_name="timeblockSet",
                                                                        entity_byname=(solve_entity["name"],),
                                                                        parameter_definition_name="block_duration",
                                                                        alternative_name=alt,
                                                                        value=p_value,
                                                                        type=p_type)
            if error:
                print("writing block_duration failed: " + error)
        for system_entity in source_db.get_entity_items(entity_class_name="system"):
            added, error = target_db.add_item("entity",
                                                entity_class_name="timeblockSet__timeline",
                                                element_name_list=[solve_entity["name"], system_entity["name"]],
                                                )
            if error:
                print("creating entity for timeblockset__timeline failed: " + error)
        period_years = defaultdict(list)
        years_represented = defaultdict(list)
        for period_entity in source_db.get_entity_items(entity_class_name="period"):
            for y_rep_value in source_db.get_parameter_value_items(entity_class_name="period",
                                                                   entity_byname=(period_entity["name"], ),
                                                                   parameter_definition_name="years_represented"):
                period_years[y_rep_value["alternative_name"]].append(period_entity["name"])
                years_reped = api.from_database(y_rep_value["value"], y_rep_value["type"])
                years_represented[y_rep_value["alternative_name"]].append(years_reped)

        for alt, value in period_years.items():
            years_map = api.Map(indexes=value, values=years_represented[alt], index_name="period")
            p_value, p_type = api.to_database(years_map)
            added, error = target_db.add_parameter_value_item(entity_class_name="solve",
                                                              entity_byname=(solve_entity["name"], ),
                                                              parameter_definition_name="years_represented",
                                                              alternative_name=alt,
                                                              type=p_type,
                                                              value=p_value)

            if error:
                exit("Not able to add years represented to the solve entity: " + error)
    try:
        target_db.commit_session("Added timeline entities and values")
    except NothingToCommit:
        pass
    except DBAPIError as e:
        print("commit timeline parameters error")
    return target_db


def params_to_dict(old_param, params):
    for param in params:
        value = api.from_database(param["value"], param["type"])
        if value:
            if param["alternative_name"] in old_param:
                old_param[param["alternative_name"]] = old_param[param["alternative_name"]] + value
            else:
                old_param[param["alternative_name"]] = value
    return old_param


if __name__ == "__main__":
    main()

