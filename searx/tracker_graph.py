"""OVERVIEW: Build a website graph from supplied tracker associations.

Abstraction: each website is a node, including websites without trackers. An
undirected edge represents a nonempty intersection of two websites' tracker
identifiers; trackers are edge metadata, not separate nodes. The graph is a
dictionary containing node and edge lists, independent of browser rendering.

REQUIRES: Callers supply the data described by each function's specification.
MODIFIES: Importing defines sample data and functions. Running this module as a
    script also writes tracker_graph_sample.json in the current directory and
    prints demonstration output; its assertions check the second sample graph.
EFFECTS: build_tracker_graph computes relationships; format_for_cytoscape adapts
    their representation without serializing or rendering them. The script
    serializes the first sample as Cytoscape elements for manual use.

This prototype uses supplied/sample data and does not collect trackers or use
live search results. Future teammate integration would supply those associations
and connect graph output to the frontend; that pipeline is not implemented here.
"""

import json


site_trackers= {
    "alpha.example":["tracker-one-example", "tracker-two.example"],
    "beta.example": ["tracker-two.example"],
    "gamma.example": []
}


def build_tracker_graph(site_trackers):
    """OVERVIEW: Compute website nodes and shared-tracker edges.

    REQUIRES: site_trackers is a mapping of website identifiers to iterables of
        hashable tracker identifiers. Shared identifiers must be sortable by
        sorted(). Iterables must support repeated traversal for pair comparisons
        when needed; the mapping must remain stable during the call.
    MODIFIES: No input mapping or reusable tracker collections are changed;
        traversing a supplied iterator can consume it.
    EFFECTS: Return a new {"nodes": [...], "edges": [...]} dictionary. Each website
        has an {"id", "label"} node in mapping iteration order. Each pair sharing
        trackers has one {"source", "target", "shared_trackers"} edge, with unique,
        sorted shared identifiers. Pair order follows mapping order, with the
        earlier website as source; this ordering does not imply direction.
        Empty input returns empty lists. No I/O is performed.
    """

    websites=list(site_trackers)
    edges =[]
    nodes=[]
    for i in range(len(websites)): # First website
        #print(i, websites[i])
        for j in range(i + 1, len(websites)): #Second website
            #print(websites[i], websites[j])
            trackers_a =site_trackers[websites[i]] #Pull the first website.
            trackers_b =site_trackers[websites[j]] #Pull the second website.
            set_tracker_a=set(trackers_a)  # Make a set
            set_tracker_b=set(trackers_b)
            shared=set_tracker_a & set_tracker_b  # Shows if any are connected.
            if shared:
                edge={"source": websites[i], "target": websites[j], "shared_trackers": sorted(shared)}
                #edges.append((websites[i], websites[j]))
                edges.append(edge)
                #print("Shared trackers:", shared)
    #print("Graph edges:" , edges)
    for website in site_trackers:
        node = {
            "id": website,
            "label": website,
        }
        nodes.append(node)
        trackers=site_trackers[website]
        #print(website, trackers)
    #print(websites)
    #print("Graph nodes:", nodes)

    graph ={
        "nodes": nodes,
        "edges": edges
    }
    #print("Complete graph:", graph)
    return graph


def format_for_cytoscape(graph):
    """OVERVIEW: Wrap graph records in Cytoscape element dictionaries.

    REQUIRES: graph provides iterable "nodes" and "edges" entries. For rendering,
        their records must satisfy Cytoscape's data format, as the website string
        identifiers in the sample graphs do; this function does not validate it.
    MODIFIES: No input records are changed; traversing iterator entries consumes
        them, if supplied.
    EFFECTS: Return a new list of {"data": record} wrappers, nodes first and then
        edges, preserving each iterable's order. Records are referenced directly,
        not copied: subsequent record mutations are visible through the wrappers.
        No JSON serialization, I/O, or rendering is performed.
    """
    elements=[]

    for node in graph["nodes"]:
        elements.append({"data": node})

    for edge in graph["edges"]:
        elements.append({"data": edge})
    return elements

if __name__ == "__main__":
    result=build_tracker_graph(site_trackers)
    print("Function result:" , result)
    graph_json=json.dumps(result, indent=2)  #Needed in order to have cytoscape to work
    print(graph_json)
    cytoscape_elements= format_for_cytoscape(result)  #Converts your Python graph into Cytoscape node and edges
    with open("tracker_graph_sample.json", "w") as file:
        json.dump(cytoscape_elements, file, indent=2) #Saves the graph data as JSON
    print("Cytoscape elements:", cytoscape_elements)

    test_sites= {
        "hospital.example": ["google-analytics.example", "ad-tracker.example"],
        "clinic.example": ["google-analytics.example"],
        "university.example":["ad-tracker.example"],
        "library.example": []
    }

    result=build_tracker_graph(test_sites)
    print("Function result:", result)

    assert len(result["nodes"])==4
    assert len(result["edges"])==2


    first_edge=result["edges"][0]
    print("First edge:" , first_edge)
    assert first_edge["shared_trackers"]==["google-analytics.example"]
    second_edge=result["edges"][1]
    print("Second edge:" ,second_edge)
    assert second_edge["shared_trackers"]==["ad-tracker.example"]
    print("All graph tests passed!")