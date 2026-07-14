from odoo import models, fields, _
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    pff_configuration_ids = fields.One2many('pff.configuration', 'lead_id',
                                            string='Configurations PFF')
    pff_config_count = fields.Integer(compute='_compute_pff_config_count')
    pff_distributor_approved = fields.Boolean(
        string="Distributeur approuvé", copy=False, readonly=True,
        help="Coché une fois la demande de distributeur approuvée (contact créé, "
             "accès portail accordé, liste de prix assignée).")

    def _compute_pff_config_count(self):
        for lead in self:
            lead.pff_config_count = len(lead.pff_configuration_ids)

    def action_pff_configure(self):
        """Bouton intelligent : ouvre la liste des produits configurés du client,
        avec le client (et l'opportunité) déjà pré-remplis pour le bouton « Nouveau »."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Configurer le produit',
            'res_model': 'pff.configuration',
            'view_mode': 'list,form',
            'domain': [('lead_id', '=', self.id)],
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_lead_id': self.id,
            },
        }

    def action_pff_new_configuration(self):
        """Bouton violet en-tête (à côté de « Nouveau devis ») : crée une
        nouvelle configuration liée à l'opportunité et ouvre directement le
        configurateur 3D."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Veuillez d'abord définir un client sur l'opportunité "
                              "avant de créer une configuration."))
        config = self.env['pff.configuration'].create({
            'partner_id': self.partner_id.id,
            'lead_id': self.id,
        })
        return config.action_open_configurator()

    # --- Approbation distributeur (portail) ---
    def _pff_distributor_pricelist(self):
        """Retrouve la liste de prix distributeur sans coder son nom en dur :
        1) paramètre système `pff_configurateur.distributor_pricelist_id` (ID) ;
        2) à défaut, la 1re liste dont le nom contient « distribut ».
        Renvoie un `product.pricelist` (éventuellement vide)."""
        Pricelist = self.env['product.pricelist']
        param = self.env['ir.config_parameter'].sudo().get_param(
            'pff_configurateur.distributor_pricelist_id')
        if param:
            pl = Pricelist.browse(int(param)).exists()
            if pl:
                return pl
        return Pricelist.search([('name', 'ilike', 'distribut')], limit=1)

    def action_pff_approve_distributor(self):
        """Approuve la demande de distributeur en un clic :
        crée/relie le contact société, accorde l'accès au portail (invitation
        courriel — le distributeur choisit lui-même son mot de passe) et assigne
        la liste de prix distributeur. Idempotent et best-effort : chaque étape
        échoue proprement (note dans le fil) sans jamais bloquer les autres."""
        self.ensure_one()
        if self.pff_distributor_approved:
            raise UserError(_("Ce distributeur a déjà été approuvé."))

        # 1) Contact société (réutilise partner_id s'il existe déjà)
        partner = self.partner_id
        if not partner:
            if not (self.partner_name or self.contact_name or self.email_from):
                raise UserError(_("Impossible de créer le contact : ni société, "
                                  "ni nom, ni courriel sur l'opportunité."))
            is_company = bool(self.partner_name)
            partner = self.env['res.partner'].create({
                'name': self.partner_name or self.contact_name or self.email_from,
                'company_type': 'company' if is_company else 'person',
                'email': self.email_from or False,
                'phone': self.phone or False,
                'customer_rank': 1,
            })
            self.partner_id = partner.id

        notes = []

        # 2) Accès portail (invitation par courriel — best effort)
        if not partner.email:
            notes.append(_("⚠️ Aucun courriel sur le contact : accès portail NON "
                           "accordé (ajoutez un courriel puis « Accorder l'accès »)."))
        elif partner.user_ids:
            notes.append(_("ℹ️ Le contact a déjà un utilisateur : accès portail inchangé."))
        else:
            try:
                wizard = self.env['portal.wizard'].with_context(
                    active_model='res.partner', active_ids=partner.ids).create({})
                for wu in wizard.user_ids.filtered(lambda u: u.partner_id == partner):
                    wu.action_grant_access()
                notes.append(_("✅ Accès portail accordé — invitation envoyée à %s.")
                             % partner.email)
            except Exception as e:  # best effort : on ne bloque jamais l'approbation
                notes.append(_("⚠️ Accès portail non accordé automatiquement (%s). "
                               "Faites-le via ⚙️ Action → Accorder l'accès au portail.") % e)

        # 3) Liste de prix distributeur (best effort)
        pricelist = self._pff_distributor_pricelist()
        if pricelist:
            partner.property_product_pricelist = pricelist.id
            notes.append(_("✅ Liste de prix assignée : %s.") % pricelist.display_name)
        else:
            notes.append(_("⚠️ Liste de prix distributeur introuvable — assignez-la "
                           "manuellement (onglet Ventes & Achats du contact)."))

        self.pff_distributor_approved = True
        self.message_post(body=_("<b>Distributeur approuvé</b><br/>")
                          + "<br/>".join(notes))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Contact distributeur'),
            'res_model': 'res.partner',
            'res_id': partner.id,
            'view_mode': 'form',
            'target': 'current',
        }
