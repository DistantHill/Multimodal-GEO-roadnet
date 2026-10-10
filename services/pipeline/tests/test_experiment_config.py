from dataclasses import replace
from pathlib import Path
import json
import copy
import tomllib

import numpy as np
import pytest

from track.v2.experiments.config import load_case, load_workspace, refine_key, TimeSelection, parse_time
from track.v2.cli.experiment import toml_text
from track.v2.errors import ConfigError
from track.v2.models import SourceBatch, RefineConfig, SpatialContext
from track.v2.trajectories.refine_trajectories import refine_vehicle
from track.v2.db.source_table import SourceTableContract

ROOT = Path(__file__).resolve().parents[1]


def raw_case():
    return {"source_table":"trajectory_points", "time_start":"2023-03-01T00:00:00+08:00",
            "time_end":"2023-03-02T00:00:00+08:00", "bbox":[[111,30],[112,31]]}


def test_case_requires_region_timezone_and_nonempty_time(tmp_path):
    for changed in ({"bbox":None}, {"time_start":"2023-03-01T00:00:00"},
                    {"time_end":"2023-02-01T00:00:00+08:00"}):
        with pytest.raises((ValueError, TypeError)):
            load_case("case", {**raw_case(),**changed}, tmp_path)
    without = raw_case(); without.pop("bbox")
    with pytest.raises(ValueError, match="exactly one"):
        load_case("case", without, tmp_path)


@pytest.mark.parametrize("empty", [None, "", "   ", []])
def test_bbox_ignores_empty_region_file(tmp_path, empty):
    case = load_case("example", {**raw_case(), "region_file": empty}, tmp_path)
    assert case.region_file is None
    assert case.geometry == load_case("example", raw_case(), tmp_path).geometry


@pytest.mark.parametrize("region", [None, "", "   ", []])
@pytest.mark.parametrize("bbox", [None, "", "   ", []])
def test_empty_region_and_bbox_require_selection(tmp_path, region, bbox):
    with pytest.raises(ValueError, match="^case example: provide exactly one region_file or bbox$"):
        load_case("example", {**raw_case(), "region_file": region, "bbox": bbox}, tmp_path)


@pytest.mark.parametrize("empty", [None, "", "   ", []])
def test_polygon_union_and_exclusive_region(tmp_path, empty):
    geometry = {"type":"FeatureCollection", "features":[{"type":"Feature", "properties":{},
        "geometry":{"type":"Polygon","coordinates":[[[111,30],[112,30],[112,31],[111,31],[111,30]]]}}]}
    (tmp_path/"region.geojson").write_text(json.dumps(geometry))
    raw=raw_case(); raw["bbox"]=empty; raw["region_file"]="region.geojson"
    case=load_case("case",raw,tmp_path)
    assert case.geometry["type"] == "Polygon"
    with pytest.raises(ValueError,match="exactly one"):
        load_case("case", {**raw,"bbox":[[111,30],[112,31]]},tmp_path)


def test_query_keeps_outside_points_until_segment_window(tmp_path):
    case=load_case("case",raw_case(),tmp_path)
    sql, params=case.source_query(SourceTableContract('trajectory_points','"trajectory_points"','bigint',True))
    assert 'ST_Covers' in sql
    assert sql.index('SUM(CASE') < sql.index('WHERE _experiment_inside')
    assert params[1:3] == (1677600000.0,1677686400.0)
    assert params[:3] == params[3:]
    only_time=TimeSelection(parse_time(raw_case()['time_start'],'start'),parse_time(raw_case()['time_end'],'end'))
    query,args=only_time.source_query(SourceTableContract('trajectory_points','"trajectory_points"','bigint',True))
    assert 'timestamp >= %s AND timestamp < %s' in query
    assert args == params[1:3]


def test_refine_preserves_spatial_exit_reentry():
    n=4
    source=SourceBatch('fixture','car',np.array([[111.,30.],[111.001,30.],[111.002,30.],[111.003,30.]]),
        np.arange(n,dtype=np.float64),np.zeros(n),np.ones(n),np.arange(n,dtype=np.int64),
        np.ones(n,dtype=np.bool_),np.full(n,'',dtype=object),{'_experiment_segment':np.array([0,0,2,2])})
    result=refine_vehicle(source,spatial_context=SpatialContext(4326,32649,{"coordinate_semantics_version":2,
        "enforce_area_of_use":False,"max_scale_error":0.1}),config=RefineConfig(100,45,135,120,0.3,300,50))
    assert [track.source_row_key.tolist() for track in result.subtracks] == [[0,1],[2,3]]
    assert all(track.vehicle_id == 'car' for track in result.subtracks)


def test_local_workspace_deep_override_and_archive_operations_ignore_case(tmp_path):
    payload=tomllib.loads((ROOT/'config/pipeline-roadnet-test.toml').read_text())
    payload['paths']['data_root']=str(tmp_path)
    payload['parser']['field_specs_path']=str(ROOT/'config/field_specs.toml')
    payload['experiment']={'case':'sample','schemes':['turn','joint']}
    payload['cases']={'sample':raw_case()}
    single = copy.deepcopy(payload['detect'])
    single['candidates'] = ['g_star']
    payload['schemes']={'turn':{'detect':single},'joint':{}}
    config=tmp_path/'experiment.toml';config.write_text(toml_text(payload))
    workspace=load_workspace(config,password='testing-secret')
    assert workspace.configs['turn'].detect.candidates == ('g_star',)
    assert workspace.configs['joint'].detect.candidates == ('g_star', 'cross')
    assert refine_key(workspace.case,workspace.configs['turn']) == refine_key(workspace.case,workspace.configs['joint'])
    assert refine_key(workspace.case,replace(workspace.configs['turn'],refine=replace(workspace.configs['turn'].refine,max_gap_s=90))) != refine_key(workspace.case,workspace.configs['turn'])
    payload['cases']['sample']={'region_file':'deleted.geojson'}
    config.write_text(toml_text(payload))
    assert load_workspace(config,password='testing-secret',validate_run=False).case is None


def test_restore_requires_explicit_conversion_preserves_old_snapshot_and_completes_legacy_scheme():
    import copy
    from track.v2.cli.experiment import restore_snapshot
    current = tomllib.loads((ROOT / 'config/pipeline-roadnet-test.toml').read_text())['detect']
    legacy = {key: value for key, value in current.items() if key not in {'candidates', 'candidate', 'cluster_algorithm', 'cluster'}}
    legacy.update(current['candidate']['g_star']['params']['default'])
    legacy.update(current['candidate']['cross']['params']['default'])
    legacy.update(use_turn_events=True, use_cross_track_candidates=True,
                  candidate_dbscan_eps_m=current['cluster']['dbscan']['params']['eps_m'],
                  candidate_dbscan_min_samples=current['cluster']['dbscan']['params']['min_samples'])
    snapshot = {'detect': legacy, 'schemes': {'turn': {'detect': {'use_cross_track_candidates': False}}}}
    original = copy.deepcopy(snapshot)
    with pytest.raises(ValueError, match='migrate-detect'):
        restore_snapshot(snapshot)
    with pytest.raises(ValueError, match='explicit turn and cluster'):
        restore_snapshot(snapshot, migrate=True)
    converted = restore_snapshot(snapshot, migrate=True, turn_algorithm='g_star', cluster_algorithm='dbscan')
    assert snapshot == original
    assert converted['detect']['candidates'] == ['g_star', 'cross']
    assert converted['schemes']['turn']['detect']['candidates'] == ['g_star']
    broken = copy.deepcopy(snapshot)
    broken['detect'].pop('turn_window')
    with pytest.raises(ConfigError, match='turn_window'):
        restore_snapshot(broken, migrate=True, turn_algorithm='g_star', cluster_algorithm='dbscan')


def test_scheme_detect_partial_configuration_is_rejected(tmp_path):
    payload = tomllib.loads((ROOT / 'config/pipeline-roadnet-test.toml').read_text())
    payload['parser']['field_specs_path'] = str(ROOT / 'config/field_specs.toml')
    from track.v2.experiments.config import load_experiment_payload
    with pytest.raises(ConfigError, match='missing required'):
        load_experiment_payload(payload, {'detect': {'candidates': ['g_star']}},
                                base_directory=ROOT / 'config', postgres_password='secret')
