// SPDX-License-Identifier: AGPL-3.0-or-Later
import cytoscape from "cytoscape";

import { Plugin } from "../Plugin.ts";

/**
 * OVERVIEW: Render the prototype's static website graph in the results page.
 *
 * Abstraction: this presentation adapter treats fetched Cytoscape elements as
 * graph data; it does not compute shared trackers or collect website data.
 * Python builds/adapts sample data, the router loads this plugin on a click,
 * and the results template supplies the button and rendering container.
 *
 * REQUIRES: The plugin loader and browser environment used by Plugin.
 * MODIFIES: Construction schedules the inherited Plugin lifecycle; run() owns
 * the fetch and rendering effects described below.
 * EFFECTS: Register the trackerGraph plugin identity and render when run.
 * The inherited lifecycle logs failures. Future teammate integration would
 * provide data from tracker collection/search results; this implementation
 * always requests the static sample and has no live-data integration.
 */
export default class TrackerGraph extends Plugin {
    public constructor() {

        super("trackerGraph"); //Calls the SearXNG parent plugin
    }

    /**
     * OVERVIEW: Load sample elements and create the Cytoscape visualization.
     * REQUIRES: If #tracker-graph exists, successful rendering needs a usable
     * container and a successful JSON response containing Cytoscape elements at
     * the static sample URL. No container is a supported no-op.
     * MODIFIES: Issues a fetch request and lets Cytoscape manage the container
     * and its rendering resources. Does not alter the server's sample file.
     * EFFECTS: Return early without fetching if the container is absent;
     * otherwise parse the sample and initialize Cytoscape with node/edge styles
     * and the cose layout (layout animation disabled). Resolve without a value.
     * Reject on a non-OK response, fetch/JSON failure, or initialization error;
     * the inherited Plugin lifecycle catches and logs rejections.
     */
    protected async run():Promise<void> {
        const container =document.getElementById("tracker-graph"); // Searches the webpage for an HTML elemnt with that ID.
            if (!container) {
                return;
            }
            const response= await fetch(
                "/static/themes/simple/tracker_graph_sample.json"
            );

            if (!response.ok) {
                throw new Error("Unable to load tracker graph data");
            }
            const graphElements= await response.json();


            cytoscape ({
                container: container,
                elements:  graphElements,

                style: [
                    {
                        selector:"node",
                        style: {
                            "background-color": "#2879B8",
                            "label": "data(label)",
                            "color": "#ffffff",
                            "text-valign": "center",
                            "text-halign": "center",
                            "width": 100,
                            "height":100,
                            "font-size": 12,
                            "text-wrap": "wrap",
                            "text-max-width": "90px",
                        }
                    },
                    {
                        selector: "edge",
                        style: {
                            "width": 3,
                            "line-color": "#7D93AF",
                            "curve-style": "bezier"
                        }
                    }
                ],
                layout:{
                    name:"cose",
                    animate: false
                }
            });
        }
        /** No-op hook; the inherited lifecycle skips it because run() returns no value. */
        protected async post(): Promise<void> {

        }

}