import json

from odoo import models, fields, api, _

FAMILIES = [
    ('battant', 'Battant'),
    ('guillotine', 'Guillotine simple'),
    ('coulissant', 'Coulissant simple'),
    ('fixe', 'Fixe'),
    ('porte_ext', "Porte d'entrée"),
    ('porte_int', 'Porte intérieure'),
    ('porte_patio', 'Porte patio'),
]


class PffConfiguration(models.Model):
    _name = 'pff.configuration'
    _description = "Commande de produits configurés PFF"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Numéro', required=True, copy=False, readonly=True,
                       default=lambda self: _('Nouveau'))
    partner_id = fields.Many2one('res.partner', string='Client', required=True, tracking=True)
    lead_id = fields.Many2one('crm.lead', string='Opportunité', readonly=True)
    date_order = fields.Datetime(string='Date de création', default=fields.Datetime.now)
    date_delivery = fields.Date(string='Date de livraison')
    state = fields.Selection([
        ('draft', 'Brouillon'),
        ('production', 'En fabrication'),
        ('done', 'Terminé'),
    ], string='Statut', default='draft', tracking=True)
    line_ids = fields.One2many('pff.configuration.line', 'configuration_id',
                               string='Produits configurés')
    currency_id = fields.Many2one('res.currency',
                                  default=lambda self: self.env.company.currency_id)
    amount_total = fields.Monetary(string='Total', compute='_compute_amount_total', store=True)
    sale_order_id = fields.Many2one('sale.order', string='Devis', readonly=True, copy=False)
    purchase_order_id = fields.Many2one('purchase.order', string="Bon d'achat verre",
                                        readonly=True, copy=False)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nouveau')) == _('Nouveau'):
                vals['name'] = self.env['ir.sequence'].next_by_code('pff.configuration') or _('Nouveau')
        return super().create(vals_list)

    @api.depends('line_ids.price_subtotal')
    def _compute_amount_total(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped('price_subtotal'))

    # --- Statuts ---
    def action_production(self):
        for rec in self:
            rec.write({'state': 'production'})
            # Automatisation : à la mise en fabrication, on crée le bon d'achat
            # verre BROUILLON (silencieux). L'acheteur assigne le fournisseur et valide.
            if not rec.purchase_order_id:
                rec._create_glass_po()

    def action_done(self):
        self.write({'state': 'done'})

    def action_draft(self):
        self.write({'state': 'draft'})

    # --- Ouvrir le configurateur 3D complet (action client OWL + iframe) ---
    def action_open_configurator(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'pff_configurator',
            'name': 'Configurateur',
            'params': {'config_id': self.id},
            'context': {'config_id': self.id},
        }

    # --- Phase C : créer un devis dans Ventes ---
    def action_create_quotation(self):
        self.ensure_one()
        tmpl = self.env.ref('pff_configurateur.product_pff_configure', raise_if_not_found=False)
        product = tmpl.product_variant_id if tmpl else False
        order = self.env['sale.order'].create({
            'partner_id': self.partner_id.id,
            'origin': self.name,
        })
        for line in self.line_ids:
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id if product else False,
                'name': line.description or dict(FAMILIES).get(line.family, ''),
                'product_uom_qty': line.qty,
                'price_unit': line.price_unit,
            })
        self.sale_order_id = order.id
        return {
            'type': 'ir.actions.act_window',
            'name': 'Devis',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_quotation(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # --- Phase D : bon d'achat verre (thermos) créé automatiquement en fabrication ---
    def _create_glass_po(self):
        """Crée un purchase.order BROUILLON avec une ligne par thermos (mesures
        capturées par le configurateur → aucun recalcul). Silencieux : renvoie
        None sans bloquer s'il n'y a pas de vitrage. Le fournisseur (défaut
        Multiver) et la confirmation restent à la charge de l'acheteur."""
        self.ensure_one()
        tmpl = self.env.ref('pff_configurateur.product_pff_thermos',
                            raise_if_not_found=False)
        product = tmpl.product_variant_id if tmpl else False
        if not product:
            return False
        po_lines = []
        for line in self.line_ids:
            if not line.thermos_json:
                continue
            try:
                data = json.loads(line.thermos_json)
            except (ValueError, TypeError):
                continue
            glass = data.get('glass') or 'Thermos'
            ep = data.get('ep') or ''
            for t in data.get('thermos', []):
                qty = (t.get('qte') or 1) * (line.qty or 1)
                name = "%s — %s × %s mm%s" % (
                    glass, t.get('w'), t.get('h'),
                    (' (ép. %s)' % ep) if ep else '',
                )
                # product_uom / date_planned / price_unit : laissés aux calculs
                # Odoo (dérivés du produit) pour rester compatible Odoo 19.
                po_lines.append((0, 0, {
                    'product_id': product.id,
                    'name': name,
                    'product_qty': qty,
                }))
        if not po_lines:
            return False
        # Fournisseur par défaut (l'acheteur peut le changer avant de valider)
        vendor = self.env.ref('pff_configurateur.res_partner_multiver',
                              raise_if_not_found=False)
        if not vendor:
            vendor = self.env['res.partner'].search([('name', '=', 'Multiver')], limit=1)
        if not vendor:
            vendor = self.env['res.partner'].create({'name': 'Multiver', 'supplier_rank': 1})
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'origin': self.name,
            'order_line': po_lines,
        })
        self.purchase_order_id = po.id
        return po

    def action_view_purchase(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': self.purchase_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class PffConfigurationLine(models.Model):
    _name = 'pff.configuration.line'
    _description = "Produit configuré PFF"
    _order = 'sequence, id'

    configuration_id = fields.Many2one('pff.configuration', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    family = fields.Selection(FAMILIES, string='Famille', required=True, default='coulissant')
    width = fields.Float(string='Largeur (mm)', default=1219)
    height = fields.Float(string='Hauteur (mm)', default=914)
    description = fields.Char(string='Désignation')
    config_json = fields.Text(string='Paramètres (JSON)')   # tout l'état du configurateur
    thermos_json = fields.Text(string='Thermos (JSON)')     # verre capturé pour le bon d'achat
    qty = fields.Integer(string='Qté', default=1)
    price_unit = fields.Float(string='Prix unitaire')
    price_subtotal = fields.Float(string='Sous-total', compute='_compute_subtotal', store=True)
    currency_id = fields.Many2one(related='configuration_id.currency_id')

    @api.depends('qty', 'price_unit')
    def _compute_subtotal(self):
        for line in self:
            line.price_subtotal = line.qty * line.price_unit

    def action_edit_line(self):
        """Reprendre la configuration de CE produit précis : ouvre le
        configurateur sur cette ligne ; seule cette ligne sera remplacée
        à la validation (les autres produits restent intacts)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'pff_configurator',
            'name': 'Configurateur',
            'params': {'config_id': self.configuration_id.id, 'line_id': self.id},
            'context': {'config_id': self.configuration_id.id, 'line_id': self.id},
        }
