from odoo import models, fields

# Libellés « côté distributeur » du statut de fabrication (portail).
_PFF_FAB_PORTAL_LABELS = {
    'draft': 'En attente',
    'production': 'En fabrication',
    'done': 'Terminé',
}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    pff_fabrication_status = fields.Char(
        string="Statut de fabrication",
        compute='_compute_pff_fabrication_status',
        help="Avancement de la fabrication de la configuration PFF liée à cette "
             "commande (affiché au distributeur sur le portail, seulement une "
             "fois le devis signé et devenu commande).")

    def _compute_pff_fabrication_status(self):
        """Reprend l'état de la `pff.configuration` reliée (via `sale_order_id`).
        `sudo` : le distributeur portail n'a pas les droits sur pff.configuration."""
        Config = self.env['pff.configuration'].sudo()
        for order in self:
            cfg = Config.search([('sale_order_id', '=', order.id)], limit=1)
            order.pff_fabrication_status = (
                _PFF_FAB_PORTAL_LABELS.get(cfg.state, '') if cfg else '')
