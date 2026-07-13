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
        this.src = "/pff_configurateur/static/configurateur.html?v=25";
        const a = this.props.action || {};
        this.configId = (a.params && a.params.config_id) || (a.context && a.context.config_id);
        // line_id défini = on reprend/remplace CETTE ligne (bouton « Reprendre »
        // par ligne). Absent = nouvelle configuration (on ajoute des lignes).
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
        // Le configurateur signale qu'il est prêt → si on reprend une ligne,
        // on lui renvoie sa config à recharger.
        if (msg.type === "pff_ready") {
            if (this.lineId && this.configId) {
                const lines = await this.orm.searchRead(
                    "pff.configuration.line",
                    [["id", "=", this.lineId]],
                    ["config_json", "qty", "sequence", "poste_assign_json"]
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
                    let poste_assign = {};
                    try {
                        poste_assign = JSON.parse(l.poste_assign_json || "{}");
                    } catch (e) {
                        poste_assign = {};
                    }
                    return { config, qty: l.qty || 1, poste_assign };
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
            // Reprise d'une ligne : on remplace la ligne éditée.
            if (this.lineId && this.existingLineIds.length) {
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
                        // Thermos/verre capturés au configurateur → base du bon d'achat
                        thermos_json: JSON.stringify({
                            glass: d.glass || "",
                            ep: d.ep || "",
                            thermos: d.thermos || [],
                        }),
                        // Liste de coupe capturée → base des feuilles de production
                        comps_json: JSON.stringify(d.comps || []),
                        // Stations choisies par morceau (clé `poste|section` → n° station)
                        // → routage des ordres de travail au « Lancer en fabrication »
                        poste_assign_json: JSON.stringify(d.poste_assign || {}),
                    };
                    // Paramètres détaillés → colonnes optionnelles de la liste.
                    // Liste blanche : un param inconnu envoyé par le configurateur
                    // est ignoré (pas de crash au create).
                    const P = d.params || {};
                    [
                        "unit", "cadre", "verre", "aspect", "col_ext", "col_int",
                        "moulure", "soufflage", "grilles", "imposte", "quinc",
                        "coupe", "moust", "sections", "ouvrant", "vantaux",
                        "panneau", "sidelights",
                    ].forEach((k) => {
                        v["param_" + k] = P[k] == null ? "" : String(P[k]);
                    });
                    // Édition d'une ligne précise : on garde sa position dans la liste.
                    if (this.lineId && this.editSequence !== false && this.editSequence != null) {
                        v.sequence = this.editSequence;
                    }
                    return v;
                });
                await this.orm.create("pff.configuration.line", vals);
            }
            this.notification.add(
                this.lineId ? "Configuration mise à jour" : "Configuration enregistrée",
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
