/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, xml } from "@odoo/owl";

/**
 * Action client qui embarque le configurateur 3D complet
 * (static/configurateur.html) dans une iframe plein écran.
 * Conserve TOUT : 3D, 2D cotée, fiche technique, découpe & composants,
 * thermos, bons de travail, assemblage, validations, unités, tous les paramètres.
 */
export class PffConfigurator extends Component {
    setup() {
        this.src = "/pff_configurateur/static/configurateur.html";
    }
}
PffConfigurator.template = xml`
    <div class="o_pff_configurator" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#ffffff;">
        <iframe t-att-src="src" style="width:100%; height:100%; border:0;" allow="fullscreen"/>
    </div>`;
PffConfigurator.props = ["*"];

registry.category("actions").add("pff_configurator", PffConfigurator);
