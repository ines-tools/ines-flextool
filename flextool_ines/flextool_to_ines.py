import sys
import yaml
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import spinedb_api as api
from spinedb_api import DatabaseMapping
from spinedb_api.exception import NothingToCommit
from sqlalchemy.exc import DBAPIError

try:
    from ines_tools import ines_transform
except ImportError:
    try:
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ines-tools" / "ines_tools"))
        import ines_transform
    except ImportError:
        sys.exit("Cannot find ines tools as an installed package or as parallel folder")

if len(sys.argv) > 1:
    url_db_in = sys.argv[1]
else:
    sys.exit("Please provide input (FlexTool) and output (INES) database urls as arguments, "
             "e.g. sqlite:///path/db_file.sqlite")
if len(sys.argv) > 2:
    url_db_out = sys.argv[2]
else:
    sys.exit("Please provide input (FlexTool) and output (INES) database urls as arguments, "
             "e.g. sqlite:///path/db_file.sqlite")

_here = Path(__file__).parent
with open(_here / 'flextool_to_ines_entities.yaml', 'r') as file:
    entities_to_copy = yaml.load(file, yaml.BaseLoader)
with open(_here / 'flextool_to_ines_parameters.yaml', 'r') as file:
    parameter_transforms = yaml.load(file, yaml.BaseLoader)
with open(_here / 'flextool_to_ines_methods.yaml', 'r') as file:
    parameter_methods = yaml.load(file, yaml.BaseLoader)


# --- small read helpers (use find() per rules.md) -------------------------------

def find_entities(db, entity_class_name):
    return db.find(db.mapped_table("entity"), entity_class_name=entity_class_name)


def find_values(db, entity_class_name, parameter_definition_name):
    return db.find(
        db.mapped_table("parameter_value"),
        entity_class_name=entity_class_name,
        parameter_definition_name=parameter_definition_name,
    )


def _first_alternative(db):
    for alt in db.find(db.mapped_table("alternative")):
        return alt["name"]
    return "Base"


# FlexTool abstract timestep name (t0001, ...) -> ISO datetime, kept in sync with the
# synthesized system.timeline so value series line up with the timeline.
TIMESTEP_TO_DATETIME = {}


def build_timestep_lookup(source_db):
    """Populate TIMESTEP_TO_DATETIME from the FlexTool timeline (abstract timesteps only)."""
    TIMESTEP_TO_DATETIME.clear()
    for pv in find_values(source_db, "timeline", "timestep_duration"):
        duration_map = api.from_database(pv["value"], pv["type"])
        if not isinstance(duration_map, api.Map):
            continue
        indexes = list(duration_map.indexes)
        values = [float(v) for v in duration_map.values]
        if not indexes or _parse_datetime(indexes[0]) is not None:
            continue
        elapsed = 0.0
        for name, hours in zip(indexes, values):
            TIMESTEP_TO_DATETIME[str(name)] = (datetime(2000, 1, 1) + timedelta(hours=elapsed)).isoformat()
            elapsed += hours


def add_value(db, entity_class_name, entity_byname, parameter_definition_name, alternative_name, value):
    p_value, p_type = api.to_database(value)
    _, _, error = db.add_update_parameter_value_item(
        entity_class_name=entity_class_name,
        entity_byname=tuple(entity_byname),
        parameter_definition_name=parameter_definition_name,
        alternative_name=alternative_name,
        value=p_value,
        type=p_type,
    )
    if error:
        print(f"Failed to add {parameter_definition_name} for {entity_byname}: {error}")


def add_entity(db, entity_class_name, entity_byname):
    _, error = db.add_entity_item(entity_class_name=entity_class_name, entity_byname=tuple(entity_byname))
    if error and "already" not in str(error):
        print(f"Failed to add entity {entity_byname} in {entity_class_name}: {error}")


def commit(db, message):
    try:
        db.commit_session(message)
    except NothingToCommit:
        pass
    except DBAPIError as e:
        print(f"commit '{message}' error: {e}")


def main():
    with DatabaseMapping(url_db_in) as source_db:
        with DatabaseMapping(url_db_out) as target_db:
            # Empty the target database
            target_db.purge_items('parameter_value')
            target_db.purge_items('entity')
            target_db.purge_items('alternative')
            target_db.purge_items('scenario')
            commit(target_db, "Purged target")

            # Copy alternatives and scenarios
            for alternative in source_db.find(source_db.mapped_table("alternative")):
                target_db.add_alternative_item(name=alternative["name"])
            for scenario in source_db.find(source_db.mapped_table("scenario")):
                target_db.add_scenario_item(name=scenario["name"])
            for sa in source_db.find(source_db.mapped_table("scenario_alternative")):
                target_db.add_scenario_alternative_item(
                    alternative_name=sa["alternative_name"],
                    scenario_name=sa["scenario_name"],
                    rank=sa["rank"],
                )
            commit(target_db, "Added scenarios and alternatives")

            print("copy entities")
            ines_transform.copy_entities(source_db, target_db, entities_to_copy)
            print("transform parameters")
            ines_transform.transform_parameters(source_db, target_db, parameter_transforms, ts_to_map=False)
            print("process methods")
            ines_transform.process_methods(source_db, target_db, parameter_methods)

            build_timestep_lookup(source_db)
            print("process node types")
            process_node_types(source_db, target_db)
            print("process commodity nodes")
            process_commodity_nodes(source_db, target_db)
            print("process flow groups")
            process_flow_groups(source_db, target_db)
            print("process capacities")
            process_capacities(source_db, target_db)
            print("process storage investments")
            process_storage_investments(source_db, target_db)
            print("process non-synchronous")
            process_non_synchronous(source_db, target_db)
            print("process reserves")
            process_reserves(source_db, target_db)
            print("process efficiency")
            process_efficiency(source_db, target_db)
            print("process user constraints")
            process_user_constraints(source_db, target_db)
            print("process stochastic / flow parameters")
            process_stochastic_parameters(source_db, target_db)
            print("process profiles")
            process_profiles(source_db, target_db)
            print("create timeline")
            create_timeline(source_db, target_db)


def process_node_types(source_db, target_db):
    """FlexTool has_storage / has_balance flags -> INES node_type (storage / balance).

    has_storage=True makes the node a storage; otherwise a truthy has_balance makes it a
    balance node. Commodity nodes are set later in process_commodity_nodes and override this.
    """
    storage_flags = defaultdict(dict)  # node -> {alt: bool}
    balance_flags = defaultdict(dict)
    for pv in find_values(source_db, "node", "has_storage"):
        storage_flags[pv["entity_name"]][pv["alternative_name"]] = bool(api.from_database(pv["value"], pv["type"]))
    for pv in find_values(source_db, "node", "has_balance"):
        balance_flags[pv["entity_name"]][pv["alternative_name"]] = bool(api.from_database(pv["value"], pv["type"]))

    for node in set(storage_flags) | set(balance_flags):
        for alt in set(storage_flags.get(node, {})) | set(balance_flags.get(node, {})):
            if storage_flags.get(node, {}).get(alt):
                add_value(target_db, "node", (node,), "node_type", alt, "storage")
            elif balance_flags.get(node, {}).get(alt):
                add_value(target_db, "node", (node,), "node_type", alt, "balance")
    commit(target_db, "Set node types")


def process_commodity_nodes(source_db, target_db):
    """FlexTool 'commodity' + commodity__node -> INES node (node_type=commodity, price, co2_content).

    In FlexTool the price / co2_content live on the 'commodity' class and reach nodes
    through the commodity__node relationship; INES stores them directly on the node.
    """
    price_by_commodity = defaultdict(dict)  # commodity -> {alt: value}
    co2_by_commodity = defaultdict(dict)
    for pv in find_values(source_db, "commodity", "price"):
        price_by_commodity[pv["entity_byname"][0]][pv["alternative_name"]] = (pv["value"], pv["type"])
    for pv in find_values(source_db, "commodity", "co2_content"):
        co2_by_commodity[pv["entity_byname"][0]][pv["alternative_name"]] = (pv["value"], pv["type"])

    seen_nodes = set()
    for ent in find_entities(source_db, "commodity__node"):
        commodity, node_name = ent["entity_byname"]
        if not target_db.get_entity_item(entity_class_name="node", entity_byname=(node_name,)):
            continue
        for alt, (value, vtype) in price_by_commodity.get(commodity, {}).items():
            add_value(target_db, "node", (node_name,), "commodity_price", alt, api.from_database(value, vtype))
        for alt, (value, vtype) in co2_by_commodity.get(commodity, {}).items():
            add_value(target_db, "node", (node_name,), "co2_content", alt, api.from_database(value, vtype))
        if node_name not in seen_nodes:
            seen_nodes.add(node_name)
            alt = _first_alternative(source_db)
            add_value(target_db, "node", (node_name,), "node_type", alt, "commodity")
    commit(target_db, "Set commodity nodes")


def process_flow_groups(source_db, target_db):
    """FlexTool group__unit__node -> INES set__unit_flow.

    unit_flow is a superclass; its member is the existing unit__to_node / node__to_unit
    entity, referenced by that entity's byname. So set__unit_flow byname is
    (set,) + flow_byname (3 elements).
    """
    flow_lookup = _flow_lookup(source_db)
    for ent in find_entities(source_db, "group__unit__node"):
        group, unit, node = ent["entity_byname"]
        flow_byname = flow_lookup.get((unit, node))
        if not flow_byname:
            continue
        add_entity(target_db, "set__unit_flow", (group,) + flow_byname)
    commit(target_db, "Added flow groups")


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "true", "1")


def process_capacities(source_db, target_db):
    """Reverse of the FlexTool capacity handling.

    virtual_unitsize (unit) -> capacity on a unit flow (output preferred, else input).
    existing (unit) -> units_existing. cumulative_max/min_capacity (unit) -> units_max/min_cumulative
    (divided back by the unitsize the forward step multiplied in). invest_cost/fixed_cost/salvage_value
    on the unit -> investment_cost/fixed_cost/salvage_value on the chosen flow (unit cost x 1000).
    """
    output_flows = defaultdict(list)
    for ent in find_entities(source_db, "unit__outputNode"):
        output_flows[ent["entity_byname"][0]].append(ent["entity_byname"])
    input_flows = defaultdict(list)
    for ent in find_entities(source_db, "unit__inputNode"):
        input_flows[ent["entity_byname"][0]].append(ent["entity_byname"])

    def flow_target(unit_name):
        if output_flows.get(unit_name):
            u, n = output_flows[unit_name][0]
            return "unit__to_node", (u, n)
        if input_flows.get(unit_name):
            u, n = input_flows[unit_name][0]
            return "node__to_unit", (n, u)
        return None, None

    unitsize = {}
    for pv in find_values(source_db, "unit", "virtual_unitsize"):
        unitsize[(pv["entity_name"], pv["alternative_name"])] = _as_float(pv["parsed_value"])

    for pv in find_values(source_db, "unit", "virtual_unitsize"):
        unit_name = pv["entity_name"]
        cls, byname = flow_target(unit_name)
        val = _as_float(pv["parsed_value"])
        if cls and val is not None:
            add_value(target_db, cls, byname, "capacity", pv["alternative_name"], val)

    for pv in find_values(source_db, "unit", "existing"):
        add_value(target_db, "unit", (pv["entity_name"],), "units_existing",
                  pv["alternative_name"], pv["parsed_value"])

    for source_param, target_param in [("cumulative_max_capacity", "units_max_cumulative"),
                                       ("cumulative_min_capacity", "units_min_cumulative"),
                                       ("invest_max_total", "units_invest_max_period"),
                                       ("invest_min_total", "units_invest_min_period"),
                                       ("invest_max_period", "units_invest_max_period"),
                                       ("invest_min_period", "units_invest_min_period")]:
        for pv in find_values(source_db, "unit", source_param):
            unit_name = pv["entity_name"]
            size = unitsize.get((unit_name, pv["alternative_name"]))
            value = _divide_value(pv["parsed_value"], size) if size else pv["parsed_value"]
            add_value(target_db, "unit", (unit_name,), target_param, pv["alternative_name"], value)

    cost_map = [("invest_cost", "investment_cost", 1000.0),
                ("fixed_cost", "fixed_cost", 1000.0),
                ("salvage_value", "salvage_value", 1000.0)]
    for source_param, target_param, mult in cost_map:
        for pv in find_values(source_db, "unit", source_param):
            unit_name = pv["entity_name"]
            cls, byname = flow_target(unit_name)
            if not cls:
                continue
            value = _multiply_value(pv["parsed_value"], mult)
            add_value(target_db, cls, byname, target_param, pv["alternative_name"], value)

    commit(target_db, "Added capacities")


def process_storage_investments(source_db, target_db):
    """Node storage investment limits (FlexTool MWh) -> INES storage-unit counts.

    Investing in a node is a storage investment, so the FlexTool MWh limits are divided
    by the node's virtual_unitsize (storage_capacity) to get storage-unit counts, mirroring
    the unit handling in process_capacities. invest_forced feeds storages_fix_cumulative and
    adds the existing storages so the fixed total reflects existing + forced-invested units.
    The *_total limits share the cumulative targets; the more precise cumulative_* value wins
    when a node defines both.
    """
    unitsize = {}
    for pv in find_values(source_db, "node", "virtual_unitsize"):
        unitsize[(pv["entity_name"], pv["alternative_name"])] = _as_float(pv["parsed_value"])
    existing = {}
    for pv in find_values(source_db, "node", "existing"):
        existing[(pv["entity_name"], pv["alternative_name"])] = pv["parsed_value"]

    count_map = [("invest_max_total", "storages_max_cumulative"),
                 ("invest_min_total", "storages_min_cumulative"),
                 ("cumulative_max_capacity", "storages_max_cumulative"),
                 ("cumulative_min_capacity", "storages_min_cumulative"),
                 ("invest_max_period", "storages_invest_max_period"),
                 ("invest_min_period", "storages_invest_min_period")]
    for source_param, target_param in count_map:
        for pv in find_values(source_db, "node", source_param):
            node_name = pv["entity_name"]
            size = unitsize.get((node_name, pv["alternative_name"]))
            value = _divide_value(pv["parsed_value"], size) if size else pv["parsed_value"]
            add_value(target_db, "node", (node_name,), target_param, pv["alternative_name"], value)

    for pv in find_values(source_db, "node", "invest_forced"):
        node_name = pv["entity_name"]
        alt = pv["alternative_name"]
        size = unitsize.get((node_name, alt))
        forced = _divide_value(pv["parsed_value"], size) if size else pv["parsed_value"]
        value = _add_values(forced, existing.get((node_name, alt)))
        add_value(target_db, "node", (node_name,), "storages_fix_cumulative", alt, value)

    commit(target_db, "Added storage investments")


def process_non_synchronous(source_db, target_db):
    """FlexTool flow is_non_synchronous (yes/no) -> INES boolean is_non_synchronous."""
    for source_class, target_class, swap in [("unit__outputNode", "unit__to_node", False),
                                             ("unit__inputNode", "node__to_unit", True)]:
        for pv in find_values(source_db, source_class, "is_non_synchronous"):
            byname = pv["entity_byname"]
            if swap:
                byname = (byname[1], byname[0])
            add_value(target_db, target_class, byname, "is_non_synchronous",
                      pv["alternative_name"], _as_bool(pv["parsed_value"]))
    commit(target_db, "Added is_non_synchronous")


def process_reserves(source_db, target_db):
    """FlexTool reserve__upDown__* -> INES reserve and *__reserve.

    FlexTool keeps reserve direction in a separate 'upDown' dimension; INES folds it into
    reserve_type. One INES reserve per FlexTool reserve: symmetric when both directions are
    used, upward/downward for a single direction, contingency when it has no reservation but
    a large_failure_ratio. Provision links (unit__node__reserve / link__node__reserve) are
    created for every existing entity (is_active is a deprecated gate, honoured only when
    explicitly "no"); direction collapses so up/down provision of the same reserve merge
    (last write wins).
    """
    directions = defaultdict(set)
    for source_class in ("reserve__upDown__group", "reserve__upDown__unit__node",
                         "reserve__upDown__connection__node"):
        for ent in find_entities(source_db, source_class):
            directions[ent["entity_byname"][0]].add(ent["entity_byname"][1])

    has_requirement = {pv["entity_byname"][0]
                       for pv in find_values(source_db, "reserve__upDown__group", "reservation")}
    large_failure = set()
    for source_class in ("reserve__upDown__unit__node", "reserve__upDown__connection__node"):
        for pv in find_values(source_db, source_class, "large_failure_ratio"):
            large_failure.add(pv["entity_byname"][0])

    def reserve_type(reserve):
        dirs = directions.get(reserve, set())
        up = any("up" in d.lower() for d in dirs)
        down = any("down" in d.lower() for d in dirs)
        if reserve not in has_requirement and reserve in large_failure:
            return "contingency"
        if up and down:
            return "symmetric"
        if down and not up:
            return "downward"
        return "upward"

    alt = _first_alternative(source_db)
    for ent in find_entities(source_db, "reserve"):
        reserve = ent["entity_byname"][0]
        add_entity(target_db, "reserve", (reserve,))
        add_value(target_db, "reserve", (reserve,), "reserve_type", alt, reserve_type(reserve))

    param_map = [("increase_reserve_ratio", "reserve_requirement_factor"),
                 ("max_share", "max_reserve_provision"),
                 ("reliability", "reserve_provision_coefficient")]
    for source_class, target_class in [("reserve__upDown__unit__node", "unit__node__reserve"),
                                       ("reserve__upDown__connection__node", "link__node__reserve")]:
        # is_active is a deprecated legacy parameter (replaced by entity-alternative
        # membership); treat an existing entity as active unless is_active is explicitly "no".
        active = {pv["entity_byname"]: _as_bool(pv["parsed_value"])
                  for pv in find_values(source_db, source_class, "is_active")}
        created = set()
        for ent in find_entities(source_db, source_class):
            byname = ent["entity_byname"]  # (reserve, upDown, unit/connection, node)
            if not active.get(byname, True):
                continue
            reserve, _dir, member, node = byname
            add_entity(target_db, target_class, (member, node, reserve))
            created.add(byname)
        for source_param, target_param in param_map:
            for pv in find_values(source_db, source_class, source_param):
                byname = pv["entity_byname"]
                if byname not in created:
                    continue
                reserve, _dir, member, node = byname
                add_value(target_db, target_class, (member, node, reserve), target_param,
                          pv["alternative_name"], pv["parsed_value"])

    for ent in find_entities(source_db, "reserve__upDown__group"):
        reserve, _dir, group = ent["entity_byname"]
        add_entity(target_db, "set__reserve", (group, reserve))
    for pv in find_values(source_db, "reserve__upDown__group", "reservation"):
        reserve, _dir, group = pv["entity_byname"]
        add_value(target_db, "set__reserve", (group, reserve), "reserve_requirement",
                  pv["alternative_name"], pv["parsed_value"])

    commit(target_db, "Added reserves")


def _multiply_value(value, mult):
    if isinstance(value, api.Map):
        value.values = [float(i) * mult for i in value.values]
        return value
    number = _as_float(value)
    return number * mult if number is not None else value


def _divide_value(value, divisor):
    if not divisor:
        return value
    if isinstance(value, api.Map):
        value.values = [round(float(i) / divisor, 6) for i in value.values]
        return value
    number = _as_float(value)
    return round(number / divisor, 6) if number is not None else value


def _add_values(value, addend):
    if addend is None:
        return value
    a = _as_float(value)
    b = _as_float(addend)
    if a is not None and b is not None:
        return round(a + b, 6)
    if isinstance(value, api.Map) and b is not None:
        value.values = [round(float(i) + b, 6) for i in value.values]
        return value
    return value


def process_efficiency(source_db, target_db):
    """Reconstruct INES unit.efficiency from FlexTool efficiency / min_load / efficiency_at_min_load."""
    min_load = {}
    for pv in find_values(source_db, "unit", "min_load"):
        min_load[(pv["entity_name"], pv["alternative_name"])] = _as_float(pv["parsed_value"])
    eff_at_min = {}
    for pv in find_values(source_db, "unit", "efficiency_at_min_load"):
        eff_at_min[(pv["entity_name"], pv["alternative_name"])] = _as_float(pv["parsed_value"])

    for pv in find_values(source_db, "unit", "efficiency"):
        key = (pv["entity_name"], pv["alternative_name"])
        eff = api.from_database(pv["value"], pv["type"])
        if key in min_load and key in eff_at_min and isinstance(eff, float):
            value = api.Map(
                indexes=[min_load[key], 1.0],
                values=[eff_at_min[key], eff],
            )
            add_value(target_db, "unit", (pv["entity_name"],), "efficiency", pv["alternative_name"], value)
        else:
            add_value(target_db, "unit", (pv["entity_name"],), "efficiency", pv["alternative_name"], eff)
    commit(target_db, "Added efficiencies")


def _flow_lookup(source_db):
    """Map a FlexTool flow entity (unit, node) to the INES flow (superclass subclass) byname.

    unit_flow is an INES superclass over unit__to_node (output, byname (unit, node)) and
    node__to_unit (input, byname (node, unit)); a unit_flow member is referenced by that byname.
    """
    lookup = {}
    for ent in find_entities(source_db, "unit__outputNode"):
        unit, node = ent["entity_byname"]
        lookup[(unit, node)] = (unit, node)
    for ent in find_entities(source_db, "unit__inputNode"):
        unit, node = ent["entity_byname"]
        lookup.setdefault((unit, node), (node, unit))
    return lookup


def process_user_constraints(source_db, target_db):
    """Reverse of the FlexTool user-constraint construction.

    Each FlexTool 'constraint' with two participating flows (constraint_flow_coefficient) becomes an
    INES unit_flow__unit_flow with a ratio + constant derived from the two flow coefficients.
    """
    flow_lookup = _flow_lookup(source_db)

    # Collect per-constraint, per-alternative the participating (flow_name, coefficient)
    participation = defaultdict(lambda: defaultdict(list))  # constraint -> alt -> [(flow_name, coeff)]
    for flow_class in ["unit__outputNode", "unit__inputNode"]:
        for pv in find_values(source_db, flow_class, "constraint_flow_coefficient"):
            coeff_map = api.from_database(pv["value"], pv["type"])
            if not isinstance(coeff_map, api.Map):
                continue
            flow_name = flow_lookup.get(tuple(pv["entity_byname"]))
            if not flow_name:
                continue
            for constraint_name, coeff in zip(coeff_map.indexes, coeff_map.values):
                participation[str(constraint_name)][pv["alternative_name"]].append(
                    (flow_name, _as_float(coeff))
                )

    sense_to_params = {
        "equal": ("equality_ratio", "equality_constant"),
        "greater_than": ("greater_than_ratio", "greater_than_constant"),
        "less_than": ("less_than_ratio", "less_than_constant"),
    }

    sense_by_constraint = {}
    for pv in find_values(source_db, "constraint", "sense"):
        sense_by_constraint[pv["entity_name"]] = api.from_database(pv["value"], pv["type"])
    constant_by_constraint = defaultdict(dict)
    for pv in find_values(source_db, "constraint", "constant"):
        constant_by_constraint[pv["entity_name"]][pv["alternative_name"]] = _as_float(pv["parsed_value"])

    for constraint_name, per_alt in participation.items():
        sense = sense_by_constraint.get(constraint_name, "equal")
        ratio_param, constant_param = sense_to_params.get(sense, sense_to_params["equal"])
        for alt, flows in per_alt.items():
            if len(flows) != 2:
                continue
            # flow with positive coefficient is treated as flow_1
            flows_sorted = sorted(flows, key=lambda f: (f[1] is None, -(f[1] or 0.0)))
            (flow1, c1), (flow2, c2) = flows_sorted[0], flows_sorted[1]
            if c1 is None or c2 is None or c2 == 0:
                continue
            ratio = round(-c1 / c2, 6)
            pair_byname = tuple(flow1) + tuple(flow2)
            add_entity(target_db, "unit_flow__unit_flow", pair_byname)
            add_value(target_db, "unit_flow__unit_flow", pair_byname, ratio_param, alt, ratio)
            constant = constant_by_constraint.get(constraint_name, {}).get(alt)
            if constant:
                add_value(target_db, "unit_flow__unit_flow", pair_byname, constant_param, alt, constant)
    commit(target_db, "Added user constraints")


def _to_timeseries(inner_map):
    """Convert an api.Map of timestamp -> value into an INES time series when possible.

    FlexTool timesteps may be datetimes (forward transform) or abstract names (t0001, ...).
    Abstract names are mapped to datetimes via TIMESTEP_TO_DATETIME so they line up with the
    system.timeline; if an index can't be resolved to a datetime the Map is returned unchanged.
    """
    if not isinstance(inner_map, api.Map):
        return inner_map
    try:
        indexes = []
        for i in inner_map.indexes:
            key = str(i)
            if key in TIMESTEP_TO_DATETIME:
                indexes.append(TIMESTEP_TO_DATETIME[key])
            elif _parse_datetime(key) is not None:
                indexes.append(key)
            else:
                return inner_map
        values = [float(v) for v in inner_map.values]
        return api.TimeSeriesVariableResolution(indexes, values, False, False)
    except (TypeError, ValueError):
        return inner_map


def _is_forecast_map(value):
    if not isinstance(value, api.Map):
        return False
    if any(str(i) == "realized" for i in value.indexes):
        return True
    return len(value.values) > 0 and all(isinstance(v, api.Map) for v in value.values)


def _split_forecast(value):
    """Return (realized_value, forecast_map) from a FlexTool forecast-structured Map."""
    realized = None
    forecast_indexes = []
    forecast_values = []
    for idx, inner in zip(value.indexes, value.values):
        if str(idx) == "realized":
            realized = _to_timeseries(inner)
        else:
            forecast_indexes.append(str(idx))
            forecast_values.append(_to_timeseries(inner))
    forecast_map = api.Map(forecast_indexes, forecast_values) if forecast_indexes else None
    return realized, forecast_map


def _pass_with_forecasts(source_db, target_db, source_class, source_param,
                         target_class, target_param, forecast_param, swap_dims=False):
    for pv in find_values(source_db, source_class, source_param):
        byname = tuple(pv["entity_byname"])
        if swap_dims:
            byname = (byname[1], byname[0])
        value = api.from_database(pv["value"], pv["type"])
        if _is_forecast_map(value):
            realized, forecast_map = _split_forecast(value)
            if realized is not None:
                add_value(target_db, target_class, byname, target_param, pv["alternative_name"], realized)
            if forecast_map is not None and forecast_param:
                add_value(target_db, target_class, byname, forecast_param, pv["alternative_name"], forecast_map)
        else:
            # FlexTool stores time-varying series as Maps; INES expects a time series.
            add_value(target_db, target_class, byname, target_param, pv["alternative_name"], _to_timeseries(value))


def process_stochastic_parameters(source_db, target_db):
    """Reverse of process_stochastic_parameters: split FlexTool realized/forecast maps into
    INES base parameters and their *_forecasts counterparts."""
    _pass_with_forecasts(source_db, target_db, "node", "inflow",
                         "node", "flow_profile", "flow_profile_forecasts")
    _pass_with_forecasts(source_db, target_db, "unit", "availability",
                         "unit", "availability", "availability_forecasts")
    _pass_with_forecasts(source_db, target_db, "connection", "availability",
                         "link", "availability", "availability_forecasts")
    _pass_with_forecasts(source_db, target_db, "connection", "efficiency",
                         "link", "efficiency", "efficiency_forecasts")
    _pass_with_forecasts(source_db, target_db, "connection", "other_operational_cost",
                         "link", "operational_cost", "operational_cost_forecasts")
    _pass_with_forecasts(source_db, target_db, "unit__outputNode", "other_operational_cost",
                         "unit__to_node", "other_operational_cost", "other_operational_cost_forecasts")
    _pass_with_forecasts(source_db, target_db, "unit__inputNode", "other_operational_cost",
                         "node__to_unit", "other_operational_cost", "other_operational_cost_forecasts",
                         swap_dims=True)
    commit(target_db, "Added stochastic / flow parameters")


def process_profiles(source_db, target_db):
    """Reverse of create_profiles.

    FlexTool profile entities linked through unit__node__profile carry a profile_method and a value
    time series (on the 'profile' entity). These map back to INES profile_limit_upper / lower / fix
    and profile_method on the corresponding unit flow (unit__to_node or node__to_unit).
    """
    method_by_suffix = {"upper_limit": "profile_limit_upper",
                        "lower_limit": "profile_limit_lower",
                        "fixed": "profile_fix"}

    # profile value time series keyed by profile entity name
    profile_values = defaultdict(dict)  # profile_name -> alt -> value
    for pv in find_values(source_db, "profile", "profile"):
        profile_values[pv["entity_name"]][pv["alternative_name"]] = api.from_database(pv["value"], pv["type"])

    # profile_method on unit__node__profile
    methods = find_values(source_db, "unit__node__profile", "profile_method")

    output_flows = {(e["entity_byname"][0], e["entity_byname"][1]) for e in find_entities(source_db, "unit__outputNode")}

    # (flow_class, byname, alt) -> set of FlexTool profile methods present
    method_sets = defaultdict(set)
    for pv in methods:
        unit, node, profile = pv["entity_byname"]
        method = api.from_database(pv["value"], pv["type"])
        target_param = method_by_suffix.get(method)
        if not target_param:
            continue
        if (unit, node) in output_flows:
            flow_class, byname = "unit__to_node", (unit, node)
        else:
            flow_class, byname = "node__to_unit", (node, unit)
        # write the profile value time series (FlexTool stores it as a Map; INES wants a time series)
        for alt, value in profile_values.get(profile, {}).items():
            add_value(target_db, flow_class, byname, target_param, alt, _to_timeseries(value))
        method_sets[(flow_class, byname, pv["alternative_name"])].add(method)

    # set an INES profile_method reflecting which limits are present
    for (flow_class, byname, alt), method_set in method_sets.items():
        if "upper_limit" in method_set and "lower_limit" in method_set:
            ines_method = "upper_and_lower_limit"
        elif "fixed" in method_set:
            ines_method = "fixed"
        elif "upper_limit" in method_set:
            ines_method = "upper_limit"
        elif "lower_limit" in method_set:
            ines_method = "lower_limit"
        else:
            continue
        add_value(target_db, flow_class, byname, "profile_method", alt, ines_method)
    commit(target_db, "Added profiles")


def _duration_hours(value):
    """Return the number of hours in an api.Duration."""
    try:
        return value.value.hours
    except AttributeError:
        return None


def _parse_datetime(index):
    """Return an ISO datetime string if index already looks like a date_time, else None."""
    text = str(index)
    try:
        np.datetime64(text)
        return text
    except (ValueError, TypeError):
        return None


def _timeline_to_timeseries(duration_map):
    """Build an INES system.timeline (date_time -> step duration) from FlexTool timestep_duration.

    FlexTool timesteps may be absolute datetimes (as produced by the forward transform) or
    abstract names (t0001, ...). Datetime indexes are kept; abstract indexes are turned into
    datetimes by accumulating the step durations from a base datetime.
    """
    indexes = list(duration_map.indexes)
    values = [float(v) for v in duration_map.values]
    if not indexes:
        return None
    if _parse_datetime(indexes[0]) is not None:
        stamps = [str(i) for i in indexes]
        return api.TimeSeriesVariableResolution(stamps, values, False, False)
    base = datetime(2000, 1, 1)
    stamps = []
    elapsed = 0.0
    for hours in values:
        stamps.append((base + timedelta(hours=elapsed)).isoformat())
        elapsed += hours
    return api.TimeSeriesVariableResolution(stamps, values, False, False)


def create_timeline(source_db, target_db):
    """Reverse of create_timeline.

    FlexTool timeline.timestep_duration -> INES system.timeline (time series).
    FlexTool solve parameters -> INES solve_pattern + period entities.
    """
    # timeline -> system
    for pv in find_values(source_db, "timeline", "timestep_duration"):
        duration_map = api.from_database(pv["value"], pv["type"])
        if isinstance(duration_map, api.Map):
            timeline = _timeline_to_timeseries(duration_map)
            if timeline is not None:
                add_value(target_db, "system", (pv["entity_name"],), "timeline", pv["alternative_name"], timeline)
    commit(target_db, "Added timelines")

    # per-solve reconstruction
    for solve in find_entities(source_db, "solve"):
        solve_name = solve["name"]

        for pv in find_values(source_db, "solve", "solve_mode"):
            if pv["entity_name"] == solve_name:
                add_value(target_db, "solve_pattern", (solve_name,), "solve_mode",
                          pv["alternative_name"], api.from_database(pv["value"], pv["type"]))

        for pv in find_values(source_db, "solve", "rolling_solve_horizon"):
            if pv["entity_name"] == solve_name:
                hours = _as_float(pv["parsed_value"])
                if hours is not None:
                    add_value(target_db, "solve_pattern", (solve_name,), "rolling_horizon",
                              pv["alternative_name"], api.Duration(f"{int(hours)}h"))

        for pv in find_values(source_db, "solve", "rolling_solve_jump"):
            if pv["entity_name"] == solve_name:
                hours = _as_float(pv["parsed_value"])
                if hours is not None:
                    add_value(target_db, "solve_pattern", (solve_name,), "rolling_jump",
                              pv["alternative_name"], api.Duration(f"{int(hours)}h"))

        # realized_periods -> solve_pattern.period ; years_represented -> period entities
        for pv in find_values(source_db, "solve", "realized_periods"):
            if pv["entity_name"] == solve_name:
                periods = api.from_database(pv["value"], pv["type"])
                add_value(target_db, "solve_pattern", (solve_name,), "period",
                          pv["alternative_name"], periods)

        for pv in find_values(source_db, "solve", "years_represented"):
            if pv["entity_name"] != solve_name:
                continue
            years_map = api.from_database(pv["value"], pv["type"])
            if not isinstance(years_map, api.Map):
                continue
            for period_name, years in zip(years_map.indexes, years_map.values):
                add_entity(target_db, "period", (str(period_name),))
                add_value(target_db, "period", (str(period_name),), "years_represented",
                          pv["alternative_name"], _as_float(years))

        # timeset blocks -> solve_pattern start_time / duration
        _reconstruct_start_and_duration(source_db, target_db, solve_name)

    commit(target_db, "Added solve patterns and periods")


def _reconstruct_start_and_duration(source_db, target_db, solve_name):
    """Rebuild solve_pattern start_time / duration arrays from FlexTool timeset blocks."""
    # timeline ordered durations, keyed by timeline name
    timeline_durations = {}
    for pv in find_values(source_db, "timeline", "timestep_duration"):
        duration_map = api.from_database(pv["value"], pv["type"])
        if isinstance(duration_map, api.Map):
            timeline_durations[pv["entity_name"]] = list(zip(
                [str(i) for i in duration_map.indexes], [float(v) for v in duration_map.values]))

    timeline_name = None
    for pv in find_values(source_db, "timeset", "timeline"):
        if pv["entity_name"] == solve_name:
            timeline_name = api.from_database(pv["value"], pv["type"])
    durations = timeline_durations.get(timeline_name)

    for pv in find_values(source_db, "timeset", "timeset_duration"):
        if pv["entity_name"] != solve_name:
            continue
        block_map = api.from_database(pv["value"], pv["type"])
        if not isinstance(block_map, api.Map):
            continue
        start_times = []
        duration_values = []
        for start_stamp, block_steps in zip(block_map.indexes, block_map.values):
            start_times.append(api.DateTime(str(start_stamp)))
            hours = _block_hours(durations, str(start_stamp), int(block_steps)) if durations else None
            if hours is not None:
                duration_values.append(api.Duration(f"{int(hours)}h"))
        if start_times:
            add_value(target_db, "solve_pattern", (solve_name,), "start_time",
                      pv["alternative_name"], api.Array(start_times))
        if len(duration_values) == len(start_times) and duration_values:
            add_value(target_db, "solve_pattern", (solve_name,), "duration",
                      pv["alternative_name"], api.Array(duration_values))


def _block_hours(durations, start_stamp, block_steps):
    """Sum the timeline step durations for block_steps steps starting at start_stamp."""
    stamps = [s for s, _ in durations]
    if start_stamp not in stamps:
        return None
    start_index = stamps.index(start_stamp)
    total = 0.0
    for i in range(start_index, min(start_index + block_steps, len(durations))):
        total += durations[i][1]
    return total


if __name__ == "__main__":
    main()
