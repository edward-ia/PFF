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
        this.src = "/pff_configurateur/static/configurateur.html?v=4";
        const a = this.props.action || {};
        this.configId = (a.params && a.params.config_id) || (a.context && a.context.config_id);
        this.resume = (a.params && a.params.resume) || (a.context && a.context.resume) || false;
        // Si défini : on ne reprend/remplace QUE cette ligne (bouton par ligne).
        this.lineId = (a.params && a.params.line_id) || (a.context && a.context.line_id) || false;
        this.existingLineIds = [];
        this.editSequence = false;
        this._onMessage = this._onMessage.bind(this);
        onMounted(() => window.addEventListener("message", this._onMessage));
        onWillUnmount(() => window.removeEventListener("message", this._onMessage));
    }

    async _onMessage(ev) {
        const msg = ev.data;
        if (!msg) {
            return;
        }
        // Le configurateur signale qu'il est prêt → en mode reprise, on lui
        // renvoie la config existante à recharger.
        if (msg.type === "pff_ready") {
            if (this.resume && this.configId) {
                // line_id → une seule ligne ; sinon toutes les lignes de la config.
                const domain = this.lineId
                    ? [["id", "=", this.lineId]]
                    : [["configuration_id", "=", this.configId]];
                const lines = await this.orm.searchRead(
                    "pff.configuration.line",
                    domain,
                    ["config_json", "qty", "sequence"]
                );
                this.existingLineIds = lines.map((l) => l.id);
                this.editSequence = lines.length ? lines[0].sequence : false;
                const items = lines.map((l) => {
                    let config = {};
                    try {
                        config = JSON.parse(l.config_json || "{}");
                    } catch (e) {
                        config = {};
                    }
                    return { config, qty: l.qty || 1 };
                });
                if (ev.source) {
                    ev.source.postMessage({ type: "pff_restore", items }, "*");
                }
            }
            return;
        }
        if (msg.type !== "pff_valider") {
            return;
        }
        const items = msg.items || [];
        if (this.configId) {
            // Mode reprise : on remplace les lignes existantes.
            if (this.resume && this.existingLineIds.length) {
                await this.orm.unlink("pff.configuration.line", this.existingLineIds);
                this.existingLineIds = [];
            }
            if (items.length) {
                const vals = items.map((d) => {
                    const v = {
                        configuration_id: this.configId,
                        family: d.family,
                        width: d.width,
                        height: d.height,
                        description: d.description,
                        qty: d.qty || 1,
                        price_unit: d.price || 0,
                        config_json: JSON.stringify(d.config || {}),
                    };
                    // Édition d'une ligne précise : on garde sa position dans la liste.
                    if (this.lineId && this.editSequence !== false && this.editSequence != null) {
                        v.sequence = this.editSequence;
                    }
                    return v;
                });
                await this.orm.create("pff.configuration.line", vals);
            }
            this.notification.add(
                this.resume ? "Configuration mise à jour" : "Configuration enregistrée",
                { type: "success" }
            );
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
