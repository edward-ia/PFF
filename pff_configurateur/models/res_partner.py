from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    pff_distributor_discount = fields.Float(
        string="Remise distributeur (%)",
        help="Remise en pourcentage appliquée automatiquement sur les lignes des "
             "devis créés par ce distributeur au portail (configurateur).")
