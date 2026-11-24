// SPDX-License-Identifier: AGPL-3.0-or-Later
import cytoscape from "cytoscape";

import { Plugin } from "../Plugin.ts";

/**
 * OVERVIEW: Render the current search's website graph in the results page.
 *
 * Abstraction: this presentation adapter treats embedded Cytoscape elements as
 * graph data; it does not compute shared trackers or collect website data.
 * Python builds the live graph, the router loads this plugin on a click,
 * and the results template supplies the button and rendering container.
 *
 * REQUIRES: The plugin loader and browser environment used by Plugin.
 * MODIFIES: Construction schedules the inherited Plugin lifecycle; run() owns
 * the rendering effects described below.
 * EFFECTS: Register the trackerGraph plugin identity and render when run.
 * The inherited lifecycle logs failures. The data is the live graph that
 * webapp.py builds for this search and results.html embeds in the page;
 * run() reads it from #tracker-graph-data and makes no network request.
 */
export default class TrackerGraph extends Plugin {
    public constructor() {

        super("trackerGraph"); //Calls the SearXNG parent plugin
    }

    /**
     * OVERVIEW: Read the embedded elements and create the Cytoscape visualization.
     * REQUIRES: If #tracker-graph exists, successful rendering needs a usable
     * container and #tracker-graph-data holding Cytoscape elements as JSON text.
     * No container is a supported no-op.
     * MODIFIES: Lets Cytoscape manage the container and its rendering
     * resources. Does not alter the embedded data.
     * EFFECTS: Return early if the container is absent; throw if
     * #tracker-graph-data is absent; otherwise parse it and initialize Cytoscape
     * with node/edge styles and the cose layout (animation disabled). Resolve
     * without a value. Reject on missing data, a JSON parse failure, or an
     * initialization error; the inherited Plugin lifecycle catches and logs it.
     */
    protected async run():Promise<void> {
        const container =document.getElementById("tracker-graph"); // Searches the webpage for an HTML elemnt with that ID.
            if (!container) {
                return;
            }
            const graphData= document.getElementById("tracker-graph-data");

            if (!graphData) {
                throw new Error("Tracker graph data not found");
            }

            const graphElements=JSON.parse(graphData.textContent || "[]");

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
