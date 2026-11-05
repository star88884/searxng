from searx.tracker_graph import build_tracker_graph, format_for_cytoscape

def test_graph_nodes():
    """Verify the node count includes the website with no trackers."""
    sample_sites= {
        "alpha.example": ["tracker-one.example"],
        "beta.example" : ["tracker-one.example"],
        "gamma.example" : []
    }
    result=build_tracker_graph(sample_sites)
    assert len(result["nodes"])==3


def test_graph_edges():
    """Verify the single shared-tracker edge, its endpoints, and its metadata."""
    sample_sites= {
        "alpha.example": ["tracker-one.example"],
        "beta.example" : ["tracker-one.example"],
        "gamma.example" : []
    }
    result=build_tracker_graph(sample_sites)
    assert len(result["edges"])==1
    first_edge=result["edges"][0]
    assert first_edge["source"]=="alpha.example"
    assert first_edge["target"]== "beta.example"
    assert first_edge["shared_trackers"]==["tracker-one.example"]

def test_no_shared_trackers():
    """Verify disjoint tracker lists produce no edges."""
    sample_sites= {
        "alpha.example": ["tracker-one.example"],
        "beta.example" : ["tracker-two.example"],
        "gamma.example" : []
    }

    result=build_tracker_graph(sample_sites)
    assert len(result["edges"])==0

def test_empty_graph():
    """Verify an empty mapping produces no nodes or edges."""
    sample_sites ={}

    result=build_tracker_graph(sample_sites)
    assert len(result["nodes"])==0
    assert len(result["edges"])==0

def test_cytoscape_format():
    """Verify two nodes and one edge produce three Cytoscape elements."""
    sample_sites = {
        "alpha.example": ["tracker-one.example"],
        "beta.example": ["tracker-one.example"]
    }

    result=build_tracker_graph(sample_sites)
    elements=format_for_cytoscape(result)
    assert len(elements)==3