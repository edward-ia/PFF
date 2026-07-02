/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, xml, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * Action client qui embarque le configurateur 3D complet
 * (static/configurateur.html) dans une iframe plein écran et synchronise
 * la configuration validée avec Odoo (pff.configuration.line).
 */
export class PffConfigurator extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        // ?v= : anti-cache — incrémenter à chaque modif de configurateur.html
        // pour forcer le navigateur à recharger le fichier statique.
        this.src = "/pff_configurateur/static/configurateur.html?v=3";
        const a = this.props.action || {};
        this.configId = (a.params && a.params.config_id) || (a.context && a.context.config_id);
        this._onMessage = this._onMessage.bind(this);
        onMounted(() => window.addEventListener("message", this._onMessage));
        onWillUnmount(() => window.removeEventListener("message", this._onMessage));
    }

    async _onMessage(ev) {
        const msg = ev.data;
        if (!msg || msg.type !== "pff_valider") {
            return;
        }
        const items = msg.items || [];
        if (this.configId && items.length) {
            const vals = items.map((d) => ({
                configuration_id: this.configId,
                family: d.family,
                width: d.width,
                height: d.height,
                description: d.description,
                qty: d.qty || 1,
                price_unit: d.price || 0,
                config_json: JSON.stringify(d.config || {}),
            }));
            await this.orm.create("pff.configuration.line", vals);
            this.notification.add("Configuration enregistrée", { type: "success" });
        }
        // Retour à la fiche de configuration
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "pff.configuration",
            res_id: this.configId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}
PffConfigurator.template = xml`
    <div class="o_pff_configurator" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#ffffff;">
        <iframe t-att-src="src" style="width:100%; height:100%; border:0;" allow="fullscreen"/>
    </div>`;
PffConfigurator.props = ["*"];

registry.category("actions").add("pff_configurator", PffConfigurator);
