import json

from markupsafe import Markup

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
    product_count = fields.Integer(string='Nb de produits',
                                   compute='_compute_product_count', store=True)
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

    @api.depends('line_ids')
    def _compute_product_count(self):
        for rec in self:
            rec.product_count = len(rec.line_ids)

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
        for item_no, line in enumerate(self.line_ids, 1):
            if not line.thermos_json:
                continue
            try:
                data = json.loads(line.thermos_json)
            except (ValueError, TypeError):
                continue
            glass = data.get('glass') or 'Thermos'
            ep = data.get('ep') or ''
            ep_s = ('%smm' % ep) if (ep and 'mm' not in str(ep)) else (ep or '')
            for t in data.get('thermos', []):
                qty = (t.get('qte') or 1) * (line.qty or 1)
                # Format Fusion : (commande/item) verre (L X H) entretoise, Epais Total.
                name = "(%s/%s) %s (%s X %s) Technoform Noir, Epais Total: %s." % (
                    self.name or '', item_no, glass, t.get('w'), t.get('h'), ep_s,
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

    # ------------------------------------------------------------------
    #  RAPPORT « Commande client » (format Fusion) — données dynamiques
    # ------------------------------------------------------------------
    def _cmd_svg(self, fam, nsec, right):
        """Petit dessin 2D « vue extérieure » (SVG) par famille — style Fusion."""
        W, H = 96, 60
        s = ['<svg viewBox="0 0 100 64" width="94" height="60" '
             'xmlns="http://www.w3.org/2000/svg">',
             '<rect x="2" y="2" width="%d" height="%d" fill="#f5f5f5" '
             'stroke="#333" stroke-width="1.5"/>' % (W, H)]
        if fam == 'battant':
            s.append('<path d="M2 2 L50 32 L2 62" fill="none" stroke="#999" stroke-width="1"/>')
            s.append('<path d="M98 2 L50 32 L98 62" fill="none" stroke="#999" stroke-width="1"/>')
        elif fam == 'guillotine':
            s.append('<line x1="2" y1="32" x2="98" y2="32" stroke="#333" stroke-width="1"/>')
            s.append('<path d="M50 54 L50 40 M44 46 L50 40 L56 46" stroke="#333" '
                     'stroke-width="1.5" fill="none"/>')
        elif fam in ('coulissant', 'porte_patio'):
            for i in range(1, max(1, nsec)):
                x = 2 + W * i / max(1, nsec)
                s.append('<line x1="%.0f" y1="2" x2="%.0f" y2="62" stroke="#333" stroke-width="1"/>' % (x, x))
            if right:
                s.append('<path d="M36 32 L62 32 M56 26 L62 32 L56 38" stroke="#333" '
                         'stroke-width="1.5" fill="none"/>')
            else:
                s.append('<path d="M62 32 L36 32 M42 26 L36 32 L42 38" stroke="#333" '
                         'stroke-width="1.5" fill="none"/>')
        elif fam in ('porte_ext', 'porte_int'):
            s.append('<rect x="16" y="10" width="66" height="46" fill="none" '
                     'stroke="#999" stroke-width="1"/>')
        s.append('</svg>')
        return Markup(''.join(s))

    def _get_commande_data(self):
        """Assemble les données de la commande client au format Fusion."""
        self.ensure_one()

        def mm2in(mm):
            return round((mm or 0) / 25.4)

        fam_labels = dict(FAMILIES)
        items = []
        for idx, l in enumerate(self.line_ids, 1):
            try:
                nsec = int(''.join(c for c in (l.param_sections or '') if c.isdigit()) or 1)
            except ValueError:
                nsec = 1
            w_in, h_in = mm2in(l.width), mm2in(l.height)
            specs = []
            if l.param_cadre:
                specs.append("CADRE : %s (%s x %s)" % (l.param_cadre, w_in, h_in))
            specs.append("SECTION : (%s x %s mm)" % (round(l.width), round(l.height)))
            if l.param_verre:
                specs.append("VERRE : %s, Technoform Noir" % l.param_verre)
            if l.param_grilles and l.param_grilles not in ('Aucun', 'none', ''):
                specs.append("CROISILLONS : %s" % l.param_grilles)
            if l.param_soufflage and l.param_soufflage not in ('Aucun', 'none', ''):
                specs.append("FINITION INT. : %s" % l.param_soufflage)
            if l.param_moust and l.param_moust not in ('Non', 'none', ''):
                specs.append("MOUSTIQUAIRE : Fibre de verre")
            if l.param_quinc:
                specs.append("QUINCAILLERIE : %s" % l.param_quinc)
            if l.param_coupe:
                specs.append("COUPE-FROID : %s" % l.param_coupe)
            items.append({
                'idx': idx,
                'name': "%s PVC / %s sections" % (fam_labels.get(l.family, l.family), nsec),
                'w_in': w_in, 'h_in': h_in, 'qty': l.qty,
                'price_unit': l.price_unit, 'subtotal': l.price_subtotal,
                'svg': self._cmd_svg(l.family, nsec, (l.param_ouvrant or 'D') != 'G'),
                'specs': specs,
            })
        sub = self.amount_total
        tps = round(sub * 0.05, 2)
        tvq = round(sub * 0.09975, 2)
        return {'items': items, 'sub': sub, 'tps': tps, 'tvq': tvq,
                'total': round(sub + tps + tvq, 2)}

    # --- Bons de travail (feuilles de production) : données pour le rapport QWeb ---
    # Routage composante → postes (chevrons), et ordre d'affichage des feuilles.
    _BT_POSTES = {
        'Cadre': ['Scie', 'Poinçon machinage', 'Soudage'],
        'Volet': ['Scie', 'Poinçon machinage', 'Soudage'],
        'Parclose': ['Scie', 'Poinçon machinage'],
        'Meneau': ['Scie', 'Poinçon machinage', 'Sous-ensemble'],
        'Renfort acier': ['Scie', 'Soudage'],
        'Soufflage': ['Scie', 'Assemblage'],
        'Moustiquaire': ['Scie', 'Assemblage'],
        'Moulure': ['Scie', 'Assemblage'],
        'Croisillons': ['Scie', 'Assemblage'],
    }
    _BT_ORDER = ['Cadre', 'Volet', 'Parclose', 'Meneau', 'Renfort acier',
                 'Soufflage', 'Moustiquaire', 'Moulure', 'Croisillons']
    _BT_SECTION = {
        'battant': 'A', 'guillotine': 'GS', 'coulissant': 'CSG,F', 'fixe': 'F',
        'porte_ext': 'PE', 'porte_int': 'PI', 'porte_patio': 'PP',
    }

    def _get_bt_data(self):
        """Prépare les données du bon de travail pour le rapport QWeb :
        feuilles de production par composante (agrégées depuis les listes de
        coupe capturées), thermos, étiquettes d'assemblage, validation."""
        self.ensure_one()
        fam_labels = dict(FAMILIES)

        def _load(txt, default):
            try:
                return json.loads(txt) if txt else default
            except (ValueError, TypeError):
                return default

        # Feuilles de production par composante
        feuilles = []
        for grp in self._BT_ORDER:
            rows = []
            for idx, line in enumerate(self.line_ids, start=1):
                for c in _load(line.comps_json, []):
                    if (c.get('grp') or '') == grp:
                        rows.append({
                            'item': idx,
                            'code': c.get('code') or '',
                            'desc': c.get('desc') or '',
                            'tiger': c.get('lng'),
                            'qte': (c.get('qte') or 1) * (line.qty or 1),
                        })
            if rows:
                feuilles.append({
                    'name': grp.upper(),
                    'postes': self._BT_POSTES.get(grp, ['Scie']),
                    'rows': rows,
                })

        # Thermos (achat)
        thermos = []
        for idx, line in enumerate(self.line_ids, start=1):
            data = _load(line.thermos_json, {})
            for t in data.get('thermos', []):
                thermos.append({
                    'item': idx,
                    'desc': '%s %s × %s mm (ép. %s)' % (
                        data.get('glass') or 'Thermos', t.get('w'), t.get('h'),
                        data.get('ep') or ''),
                    'qte': (t.get('qte') or 1) * (line.qty or 1),
                })

        # Étiquettes d'assemblage + validation
        etiquettes, validation = [], []
        num = ''.join(ch for ch in (self.name or '') if ch.isdigit()) or '0'
        for idx, line in enumerate(self.line_ids, start=1):
            bullets = []
            for label, val in (('CADRE', line.param_cadre), ('VERRE', line.param_verre),
                               ('FINITION INT.', line.param_soufflage),
                               ('QUINCAILLERIE', line.param_quinc),
                               ('MOUSTIQUAIRE', line.param_moust),
                               ('COUPE-FROID', line.param_coupe)):
                if val and val not in ('Aucun', 'Aucune', 'Non'):
                    bullets.append('%s : %s' % (label, val))
            etiquettes.append({
                'item': idx,
                'commande': self.name or '',
                'type': fam_labels.get(line.family, line.family),
                'section': self._BT_SECTION.get(line.family, ''),
                'width': int(line.width or 0),
                'height': int(line.height or 0),
                'family': line.family,
                'bullets': bullets,
                'barcode': 'S%s%02dTA' % (num[-4:].zfill(4), idx),
            })
            validation.append({
                'item': idx,
                'type': fam_labels.get(line.family, line.family),
                'width': int(line.width or 0),
                'height': int(line.height or 0),
                'egress': 'egress' in (line.description or '').lower(),
            })

        return {'feuilles': feuilles, 'thermos': thermos,
                'etiquettes': etiquettes, 'validation': validation}


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
    comps_json = fields.Text(string='Liste de coupe (JSON)')  # pièces capturées pour les feuilles de production
    qty = fields.Integer(string='Qté', default=1)
    price_unit = fields.Float(string='Prix unitaire')
    price_subtotal = fields.Float(string='Sous-total', compute='_compute_subtotal', store=True)
    currency_id = fields.Many2one(related='configuration_id.currency_id')

    # --- Paramètres détaillés (libellés lisibles remplis par le configurateur) ---
    # Copies dénormalisées de config_json, uniquement pour l'affichage en
    # colonnes optionnelles dans la liste. Source de vérité = config_json.
    param_unit = fields.Char(string="Unité")
    param_cadre = fields.Char(string="Cadre")
    param_verre = fields.Char(string="Verre / Thermos")
    param_aspect = fields.Char(string="Aspect du verre")
    param_col_ext = fields.Char(string="Couleur ext.")
    param_col_int = fields.Char(string="Couleur int.")
    param_moulure = fields.Char(string="Moulure ext.")
    param_soufflage = fields.Char(string="Soufflage int.")
    param_grilles = fields.Char(string="Croisillons")
    param_imposte = fields.Char(string="Imposte")
    param_quinc = fields.Char(string="Quincaillerie")
    param_coupe = fields.Char(string="Coupe-froid")
    param_moust = fields.Char(string="Moustiquaire")
    param_sections = fields.Char(string="Sections")
    param_ouvrant = fields.Char(string="Sens d'ouverture")
    param_vantaux = fields.Char(string="Vantaux")
    param_panneau = fields.Char(string="Type de panneau")
    param_sidelights = fields.Char(string="Verres latéraux")

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
