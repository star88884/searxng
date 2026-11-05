// SPDX-License-Identifier: AGPL-3.0-or-Later
import cytoscape from "cytoscape";

import { Plugin } from "../Plugin.ts";

/**
 * OVERVIEW: Render the website graph of the current search in the results page.
 *
 * Abstraction: this presentation adapter treats embedded Cytoscape elements as
 * graph data; it does not compute shared trackers or collect website data.
 * Python builds the graph from the search's tracker_lens table (the
 * tracker_graph filter in webapp.py), the results template embeds it as JSON
 * in #tracker-graph-data beside the button and rendering container, and the
 * router loads this plugin on a click.
 *
 * REQUIRES: The plugin loader and browser environment used by Plugin.
 * MODIFIES: Construction schedules the inherited Plugin lifecycle; run() owns
 * the fetch and rendering effects described below.
 * EFFECTS: Register the trackerGraph plugin identity and render when run.
 * The inherited lifecycle logs failures.
 */
export default class TrackerGraph extends Plugin {
    public constructor() {

        super("trackerGraph"); //Calls the SearXNG parent plugin
    }

    /**
     * OVERVIEW: Load sample elements and create the Cytoscape visualization.
     * REQUIRES: If #tracker-graph and #tracker-graph-data both exist, successful
     * rendering needs a usable container and Cytoscape elements as JSON text in
     * #tracker-graph-data. A missing element is a supported no-op (the template
     * leaves the data out when tracker_lens is off).
     * MODIFIES: Lets Cytoscape manage the container and its rendering resources.
     * Does not alter the embedded data.
     * EFFECTS: Return early if either element is absent; otherwise parse the
     * embedded elements and initialize Cytoscape with node/edge styles and the
     * cose layout (layout animation disabled). Resolve without a value.
     * Reject on a JSON parse failure or initialization error; the inherited
     * Plugin lifecycle catches and logs rejections.
     */
    protected async run():Promise<void> {
        const container =document.getElementById("tracker-graph"); // Searches the webpage for an HTML elemnt with that ID.
        const data = document.getElementById("tracker-graph-data"); // The graph the results page embeds.
            if (!container || !data) {
                return;
            }
            const graphElements = JSON.parse(data.textContent ?? "[]");


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