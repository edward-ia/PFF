from odoo import models, fields


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    pff_configuration_ids = fields.One2many('pff.configuration', 'lead_id',
                                            string='Configurations PFF')
    pff_config_count = fields.Integer(compute='_compute_pff_config_count')

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
