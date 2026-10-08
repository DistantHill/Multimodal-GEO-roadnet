import pytest
from pipeline.experiments.evaluation import evaluate, _matching


def polygon(identifier, x=0, y=0, width=.001):
    return {"type": "Feature", "id": identifier, "properties": {}, "geometry": {
        "type": "Polygon", "coordinates": [[[x,y],[x+width,y],[x+width,y+width],[x,y+width],[x,y]]]}}


def test_cardinality_precedes_distance():
    assert set(_matching(2, 2, [(0,0,1), (0,1,2), (1,0,2)])) == {(0,1),(1,0)}


def test_minimum_cost_among_maximum_matches():
    assert set(_matching(2,2, [(0,0,1),(0,1,4),(1,0,4),(1,1,1)])) == {(0,0),(1,1)}


def test_duplicate_prediction_is_false_positive():
    result = evaluate([polygon("p1"),polygon("p2")], [polygon("t")],
                      {"mode":"center_distance", "distance_m":10})
    assert result["metrics"] == {"tp":1,"fp":1,"fn":0,"precision":.5,"recall":1,"f1":2/3}
    assert result["matches"][0]["prediction_id"] == "p1"


def test_empty_metrics_undefined():
    assert evaluate([],[],{"mode":"iou","iou_threshold":.5})["metrics"] == {
        "tp":0,"fp":0,"fn":0,"precision":None,"recall":None,"f1":None}


def test_iou_and_combined_rule():
    predicted, truth = [polygon("p")], [polygon("t",.0002)]
    assert evaluate(predicted,truth,{"mode":"iou","iou_threshold":.5})["metrics"]["tp"] == 1
    assert evaluate(predicted,truth,{"mode":"both","iou_threshold":.5,"distance_m":1})["metrics"]["tp"] == 0


def test_truth_ids_are_required_and_unique():
    with pytest.raises(ValueError, match="unique"):
        evaluate([], [polygon("t"), polygon("t")], {"mode":"iou","iou_threshold":.5})


@pytest.mark.parametrize("rules", [{"mode":"both","distance_m":10},
    {"mode":"iou","iou_threshold":float("nan")}, {"mode":"center_distance","distance_m":0}])
def test_thresholds_required(rules):
    with pytest.raises(ValueError):
        evaluate([],[],rules)


@pytest.mark.parametrize("collection", [{}, {"type":"FeatureCollection"},
    {"type":"FeatureCollection","features":{}}, [None], [{"type":"Point"}]])
def test_malformed_geojson_rejected(collection):
    with pytest.raises(ValueError):
        evaluate(collection,[],{"mode":"iou","iou_threshold":.5})


@pytest.mark.parametrize("value", [True,"10",None])
def test_numeric_threshold_type(value):
    with pytest.raises(ValueError):
        evaluate([],[],{"mode":"center_distance","distance_m":value})
