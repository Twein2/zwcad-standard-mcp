;;; mo_tools.lsp -- architectural helpers called by zwcad-standard-mcp tools (MIT licence)
;;;
;;; Non-interactive cores ported from the office LISP routines of the fork owner
;;; (WklejPole, WklejPoleHatch, LinijkaSlonca, CAYENNE/SCIEZKA). The original interactive commands are
;;; not changed or required; these functions take handles, points and options instead of prompts.
;;; All inputs are WCS. Functions return assoc lists (serialized to JSON by zwmcp:json).
;;; Keep this file ASCII-only.

;;; =====================================================================================
;;; Area field (from WklejPole.lsp / WklejPoleHatch.lsp)
;;; =====================================================================================
;;; Puts a live area field of a polyline / hatch / circle / region into an MTEXT (or TEXT),
;;; keeping the existing text in front of the old area value, e.g. "POKOJ 1 12.5 m2".
;;; opts:
;;;   "format"     field format string, e.g. "%lu2%pr1%ct8[.0001]" (cm drawing -> m2, 1 decimal)
;;;   "with_layer" T -> "<layer> - <area>" (WklejPoleHatch behaviour)
;;;   "keep_text"  T (default) keep the text before the old value, nil -> replace the whole text
;;;   "suffix"     text after the field; default MTEXT superscript m2

(defun mo:area-cut-prefix (curr / pos i cut ch)
  ;; Same cut rule as WklejPole: text before an existing field, or before the old "<value>m2" suffix.
  (if (setq pos (vl-string-search "%<" curr))
    (setq curr (substr curr 1 pos))
    (if (setq pos (vl-string-search "m{\\H0.7x;\\S2^;}" curr))
      (progn
        (setq i (1- pos) cut nil)
        (while (and (>= i 0) (= (substr curr (1+ i) 1) " ")) (setq i (1- i)))
        (while (and (>= i 0) (not cut))
          (setq ch (substr curr (1+ i) 1))
          (if (or (= ch " ") (= ch ";") (= ch "P") (= ch "\\")) (setq cut (1+ i)))
          (setq i (1- i))
        )
        (setq curr (if cut (substr curr 1 cut) ""))
      )
    )
  )
  (vl-string-right-trim " " curr)
)

(defun mo:area-field (txt-h src-h opts / et es vt vs tname sname id fmt fld curr ext suffix new mtext)
  (setq et (zwmcp:ent txt-h) es (zwmcp:ent src-h)
        vt (vlax-ename->vla-object et) vs (vlax-ename->vla-object es)
        tname (vla-get-ObjectName vt) sname (vla-get-ObjectName vs))
  (if (not (wcmatch (strcase tname) "*TEXT"))
    (zwmcp:fail (strcat "Target " txt-h " is " tname ", expected MTEXT or TEXT")))
  (if (not (wcmatch (strcase sname) "*POLYLINE,*HATCH,*CIRCLE,*REGION,*ELLIPSE,*SPLINE"))
    (zwmcp:fail (strcat "Area source " src-h " is " sname ", expected a closed polyline, hatch, circle, region, ellipse or spline")))
  (setq mtext (wcmatch (strcase tname) "*MTEXT")
        id (vl-princ-to-string (vla-get-ObjectID vs))
        fmt (zwmcp:opt "format" opts "%lu2%pr1%ct8[.0001]")
        fld (strcat "%<\\AcObjProp Object(%<\\_ObjId " id ">%).Area \\f \"" fmt "\">%"))
  (if (zwmcp:opt "with_layer" opts nil)
    (setq fld (strcat "%<\\AcObjProp Object(%<\\_ObjId " id ">%).Layer>% - " fld)))
  (setq suffix (zwmcp:opt "suffix" opts (if mtext "m{\\H0.7x;\\S2^;}" " m2")))
  (setq curr (vl-catch-all-apply 'vla-FieldCode (list vt)))
  (if (vl-catch-all-error-p curr) (setq curr (vla-get-TextString vt)))
  (setq curr (if (zwmcp:opt "keep_text" opts T) (mo:area-cut-prefix curr) ""))
  ;; Remove the old field dictionary first, otherwise overwriting a field eats \A / \f codes.
  (setq ext (vl-catch-all-apply 'vla-GetExtensionDictionary (list vt)))
  (if (not (vl-catch-all-error-p ext))
    (vl-catch-all-apply 'vla-Remove (list ext "ACAD_FIELD")))
  (setq new (if (= curr "") (strcat fld suffix) (strcat curr " " fld suffix)))
  (vla-put-TextString vt new)
  (command "_.UPDATEFIELD" et "")
  (vla-update vt)
  (list (cons "success" T)
        (cons "text_handle" txt-h)
        (cons "source_handle" src-h)
        (cons "source_type" sname)
        (cons "area" (vl-catch-all-apply 'vla-get-Area (list vs)))
        (cons "kept_text" curr)
        (cons "text" (vla-get-TextString vt)))
)

;; pairs: list of (text-handle source-handle); one result per pair, failures do not stop the batch.
(defun mo:area-fields (pairs opts / out r cmd)
  (setq cmd (getvar "CMDECHO"))
  (setvar "CMDECHO" 0)
  (foreach p pairs
    (setq zwmcp:*last-error* nil
          r (vl-catch-all-apply 'mo:area-field (list (car p) (cadr p) opts)))
    (setq out
      (cons
        (if (vl-catch-all-error-p r)
          (list (cons "success" 'false) (cons "text_handle" (car p)) (cons "source_handle" (cadr p))
                (cons "error" (if zwmcp:*last-error* zwmcp:*last-error* (vl-catch-all-error-message r))))
          r)
        out))
  )
  (setvar "CMDECHO" cmd)
  (setq zwmcp:*last-error* nil)
  (reverse out)
)

;;; =====================================================================================
;;; Sun exposure ruler (from LinijkaSlonca_2026.09.14.lsp)
;;; =====================================================================================
;;; Equinox shadow ruler (21 March / 23 September) for the WT 13 h-of-sun check: shadow lines every
;;; 30 min between 7:00 and 17:00 for an obstruction of height H above the terrain level, height
;;; lines 1..5 m, the exposure sector between two directions (or times) as a 50 % transparent SOLID,
;;; everything turned into one block, plus optional result text and room label.
;;; p (assoc list):
;;;   "base" (x y) | "equinox" "W" / "J" | "h_obj" "h_ter" metres | "phi" "lambda" degrees
;;;   "scale" drawing units per metre | "txt_h_hours" "txt_h_desc" drawing units
;;;   "start_dir" (x y) or "start_time" hours | "end_dir" (x y) or "end_time" hours
;;;   "desc_point" (x y) or nil | "room" string or nil, "room_point" (x y)

(defun mo:sun-d2r (d) (* pi (/ d 180.0)))
(defun mo:sun-r2d (r) (* r (/ 180.0 pi)))
(defun mo:sun-tan (x) (/ (sin x) (cos x)))
(defun mo:sun-asin (x) (atan x (sqrt (- 1.0 (* x x)))))
(defun mo:sun-acos (x)
  (cond ((>= x 1.0) 0.0) ((<= x -1.0) pi) (t (- (/ pi 2.0) (mo:sun-asin x))))
)
(defun mo:sun-fmt (t_dec / t_min th tm)
  (setq t_min (fix (+ (* t_dec 60.0) 0.5)) th (/ t_min 60) tm (rem t_min 60))
  (strcat (itoa th) ":" (if (< tm 10) (strcat "0" (itoa tm)) (itoa tm)))
)

;; azimuth/altitude of the sun at local time hr (uses rad_phi lam tz eot of the caller)
(defun mo:sun-pos (hr / ast omega sin_alt alt_rad cos_az az_deg)
  (setq ast (+ hr (/ (- lam (* 15.0 tz)) 15.0) eot)
        omega (mo:sun-d2r (* (- ast 12.0) 15.0))
        sin_alt (* (cos rad_phi) (cos omega))
        alt_rad (mo:sun-asin sin_alt)
        cos_az (* -1.0 (mo:sun-tan alt_rad) (mo:sun-tan rad_phi)))
  (if (> cos_az 1.0) (setq cos_az 1.0))
  (if (< cos_az -1.0) (setq cos_az -1.0))
  (setq az_deg (mo:sun-r2d (mo:sun-acos cos_az)))
  (if (> omega 0.0) (setq az_deg (- 360.0 az_deg)))
  (list az_deg (mo:sun-r2d alt_rad))
)

;; shadow direction (CAD angle, radians) and length (drawing units) at time hr
(defun mo:sun-shadow (hr / pos)
  (setq pos (mo:sun-pos hr))
  (list (mo:sun-d2r (- 90.0 (car pos)))
        (if (> (cadr pos) 0.0) (/ (* h sc) (mo:sun-tan (mo:sun-d2r (cadr pos)))) 0.0))
)

;; local time for a shadow pointing along CAD angle cad_ang (degrees), as in LinijkaSlonca
(defun mo:sun-time-for-angle (cad_ang / az tan_alt alt_rad cos_omega omega ast)
  (setq az (- 90.0 cad_ang))
  (while (< az 0.0) (setq az (+ az 360.0)))
  (while (>= az 360.0) (setq az (- az 360.0)))
  (setq tan_alt (/ (* -1.0 (cos (mo:sun-d2r az))) (/ (sin rad_phi) (cos rad_phi)))
        alt_rad (atan tan_alt)
        cos_omega (/ (sin alt_rad) (cos rad_phi)))
  (if (> cos_omega 1.0) (setq cos_omega 1.0))
  (if (< cos_omega -1.0) (setq cos_omega -1.0))
  (setq omega (mo:sun-r2d (mo:sun-acos cos_omega)))
  (setq ast (if (< az 180.0) (- 12.0 (/ omega 15.0)) (+ 12.0 (/ omega 15.0))))
  (- ast (+ (/ (- lam (* 15.0 tz)) 15.0) eot))
)

(defun mo:sun-layer (name color plot)
  (if (not (tblsearch "LAYER" name))
    (entmake (append (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord") '(100 . "AcDbLayerTableRecord")
                           (cons 2 name) '(70 . 0) (cons 62 color))
                     (if plot nil (list '(290 . 0)))))
  )
)

(defun mo:sun-mk (data / e)
  (if (entmake data)
    (progn
      (setq e (entlast))
      (setq lst_to_block (cons e lst_to_block))
      e
    )
  )
)

(defun mo:sun-ruler (p / pt phi lam end_pt doc eq_choice h sc tz eot rad_phi txt_h txt_d hr data hour_ents
                       lst_to_block item e1 e2 sh curr_h pt_start pt_end pos_s pos_e time1 time2 dir
                       end_pt1 end_pt2 solid_ent eq_text blk_name orig_blk_name idx ss ins cmd
                       delta_time total_min h_dur m_dur desc_ent room_ent room pt_text kept dashed)
  (setq pt (zwmcp:pt3 (zwmcp:opt "base" p nil))
        phi (float (zwmcp:opt "phi" p 51.0))
        lam (float (zwmcp:opt "lambda" p 17.0))
        eq_choice (zwmcp:opt "equinox" p "W")
        sc (float (zwmcp:opt "scale" p 1.0))
        txt_h (float (zwmcp:opt "txt_h_hours" p (* 0.25 sc)))
        txt_d (float (zwmcp:opt "txt_h_desc" p (* 0.5 sc)))
        h (- (float (zwmcp:opt "h_obj" p 15.0)) (float (zwmcp:opt "h_ter" p 0.0))))
  (if (<= h 0.0) (zwmcp:fail "Obstruction height must be above the terrain level (h_obj > h_ter)."))
  (if (= eq_choice "W") (setq tz 1.0 eot -0.123) (setq tz 2.0 eot 0.126))
  (setq rad_phi (mo:sun-d2r phi))

  (mo:sun-layer "a_niedrukowalne_naslonecznienie" 8 nil)
  (mo:sun-layer "a_naslonecznienie_kolor_1" 50 T)
  (mo:sun-layer "a_naslonecznienie_obszar" 40 T)
  (mo:sun-layer "a_naslonecznienie_opis" 7 T)
  (mo:sun-layer "a_naslonecznienie_lokal" 3 T)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
  (if (not (tblsearch "LTYPE" "DASHED"))
    (foreach lin '("zwcadiso.lin" "zwcad.lin" "acadiso.lin")
      (if (not (tblsearch "LTYPE" "DASHED"))
        (vl-catch-all-apply 'vla-Load (list (vla-get-Linetypes doc) "DASHED" lin)))))
  (setq dashed (tblsearch "LTYPE" "DASHED"))

  ;; shadow lines every 30 min, 7:00-17:00
  (setq hr 7.0)
  (while (<= hr 17.0)
    (setq sh (mo:sun-shadow hr))
    (if (> (cadr sh) 0.0)
      (progn
        (setq end_pt (polar pt (car sh) (cadr sh)))
        (setq e1 (mo:sun-mk (append (list '(0 . "LINE") '(8 . "a_naslonecznienie_kolor_1") (cons 10 pt) (cons 11 end_pt))
                                    (if (and dashed (/= hr (fix hr))) (list (cons 6 "DASHED")) nil))))
        (setq e2 (mo:sun-mk (list '(0 . "TEXT") '(8 . "a_naslonecznienie_kolor_1") (cons 10 end_pt) (cons 40 txt_h)
                                  (cons 1 (mo:sun-fmt hr)) (cons 50 (/ pi 2.0)))))
        (setq hour_ents (cons (list hr e1 e2) hour_ents))
      )
    )
    (setq hr (+ hr 0.5))
  )

  ;; height lines 1..5 m (non-plotting layer)
  (setq pos_s (mo:sun-pos 7.0) pos_e (mo:sun-pos 17.0) curr_h 1.0)
  (while (<= curr_h 5.0)
    (setq pt_start (polar pt (mo:sun-d2r (- 90.0 (car pos_s))) (/ (* curr_h sc) (mo:sun-tan (mo:sun-d2r (cadr pos_s)))))
          pt_end (polar pt (mo:sun-d2r (- 90.0 (car pos_e))) (/ (* curr_h sc) (mo:sun-tan (mo:sun-d2r (cadr pos_e)))))
          pt_end (list (car pt_end) (cadr pt_start) (caddr pt_start)))
    (mo:sun-mk (list '(0 . "LINE") '(8 . "a_niedrukowalne_naslonecznienie") (cons 10 pt_start) (cons 11 pt_end)))
    (mo:sun-mk (list '(0 . "TEXT") '(8 . "a_niedrukowalne_naslonecznienie")
                     (cons 10 (list (+ (car pt_start) (* txt_h 0.6)) (+ (cadr pt_start) (* txt_h 0.4)) (caddr pt_start)))
                     (cons 40 txt_h) (cons 1 (strcat (rtos curr_h 2 0) "m")) (cons 50 0.0)))
    (setq curr_h (+ curr_h 1.0))
  )

  ;; exposure start / end: direction point, else time, else 7:00 / 17:00
  (setq dir (zwmcp:opt "start_dir" p nil))
  (setq time1 (if (and dir (> (distance pt (zwmcp:pt3 dir)) 0.0))
                (mo:sun-time-for-angle (mo:sun-r2d (angle pt (zwmcp:pt3 dir))))
                (float (zwmcp:opt "start_time" p 7.0))))
  (setq dir (zwmcp:opt "end_dir" p nil))
  (setq time2 (if (and dir (> (distance pt (zwmcp:pt3 dir)) 0.0))
                (mo:sun-time-for-angle (mo:sun-r2d (angle pt (zwmcp:pt3 dir))))
                (float (zwmcp:opt "end_time" p 17.0))))
  (setq time1 (min 17.0 (max 7.0 time1)) time2 (min 17.0 (max 7.0 time2)))

  (setq sh (mo:sun-shadow time1) end_pt1 (polar pt (car sh) (cadr sh)))
  (mo:sun-mk (list '(0 . "LINE") '(8 . "a_naslonecznienie_kolor_1") (cons 10 pt) (cons 11 end_pt1)))
  (mo:sun-mk (list '(0 . "TEXT") '(8 . "a_naslonecznienie_kolor_1") (cons 10 end_pt1) (cons 40 txt_h)
                   (cons 1 (mo:sun-fmt time1)) (cons 50 (/ pi 2.0))))
  (setq sh (mo:sun-shadow time2) end_pt2 (polar pt (car sh) (cadr sh)))
  (mo:sun-mk (list '(0 . "LINE") '(8 . "a_naslonecznienie_kolor_1") (cons 10 pt) (cons 11 end_pt2)))
  (mo:sun-mk (list '(0 . "TEXT") '(8 . "a_naslonecznienie_kolor_1") (cons 10 end_pt2) (cons 40 txt_h)
                   (cons 1 (mo:sun-fmt time2)) (cons 50 (/ pi 2.0))))

  ;; drop the half-hour lines outside the exposure window (same rule as LinijkaSlonca)
  (foreach item hour_ents
    (if (or (< (car item) (min time1 time2)) (> (car item) (max time1 time2)))
      (foreach e (cdr item)
        (if (and e (entget e)) (progn (entdel e) (setq lst_to_block (vl-remove e lst_to_block))))
      )
    )
  )

  ;; exposure sector, 50 % transparent, sent to the back
  (setq solid_ent (mo:sun-mk (list '(0 . "SOLID") '(8 . "a_naslonecznienie_obszar")
                                   (cons 10 pt) (cons 11 end_pt1) (cons 12 end_pt2) (cons 13 end_pt2))))
  (vl-catch-all-apply 'vla-put-EntityTransparency (list (vlax-ename->vla-object solid_ent) "50"))

  (setq cmd (getvar "CMDECHO"))
  (setvar "CMDECHO" 0)
  (setq eq_text (if (= eq_choice "W") "21 marca" "23 wrzesnia"))
  (if (zwmcp:opt "make_block" p T)
    (progn
      (setq blk_name (strcat (vl-string-translate " " "_" eq_text) "_H_" (vl-string-translate "." "_" (rtos h 2 2)) "m_"
                             (vl-string-translate "." "_" (rtos (getvar "CDATE") 2 6)))
            idx 1 orig_blk_name blk_name)
      (while (tblsearch "BLOCK" blk_name)
        (setq blk_name (strcat orig_blk_name "_" (itoa idx)) idx (1+ idx)))
      (setq ss (ssadd))
      (foreach e lst_to_block (if (and e (entget e)) (ssadd e ss)))
      (vl-cmdf "_.DRAWORDER" solid_ent "" "_B")
      (vl-cmdf "_.-BLOCK" blk_name "_non" pt ss "")
      (vl-cmdf "_.-INSERT" blk_name "_non" pt 1 1 0)
      (setq ins (entlast))
      (vl-cmdf "_.DRAWORDER" ins "" "_B")
    )
    (vl-cmdf "_.DRAWORDER" solid_ent "" "_B")
  )
  (setvar "CMDECHO" cmd)

  (setq delta_time (abs (- time1 time2))
        total_min (fix (+ (* delta_time 60.0) 0.5))
        h_dur (/ total_min 60) m_dur (rem total_min 60))

  (if (setq pt_text (zwmcp:opt "desc_point" p nil))
    (progn
      (entmake (list '(0 . "MTEXT") '(100 . "AcDbEntity") '(100 . "AcDbMText") '(8 . "a_naslonecznienie_opis")
                     (cons 10 (zwmcp:pt3 pt_text)) (cons 40 txt_d) (cons 71 1) (cons 11 '(1.0 0.0 0.0))
                     (cons 1 (strcat "Rownonoc: " eq_text ", H = " (rtos h 2 2) "m (liczac od "
                                     (rtos (float (zwmcp:opt "h_ter" p 0.0)) 2 2) "m)"
                                     "\\P" "Godziny ekspozycji: od " (mo:sun-fmt (min time1 time2))
                                     " do " (mo:sun-fmt (max time1 time2))
                                     " (" (itoa h_dur) " godz. " (itoa m_dur) " min.)"))))
      (setq desc_ent (entlast))
    )
  )
  (if (and (setq room (zwmcp:opt "room" p nil)) (/= room "") (zwmcp:opt "room_point" p nil))
    (progn
      (entmake (list '(0 . "MTEXT") '(100 . "AcDbEntity") '(100 . "AcDbMText") '(8 . "a_naslonecznienie_lokal")
                     (cons 10 (zwmcp:pt3 (zwmcp:opt "room_point" p nil))) (cons 40 txt_d) (cons 71 1)
                     (cons 11 '(1.0 0.0 0.0))
                     (cons 1 (strcat "lokal " room ": " (mo:sun-fmt (min time1 time2)) "-" (mo:sun-fmt (max time1 time2))
                                     " (" (itoa h_dur) ":" (if (< m_dur 10) (strcat "0" (itoa m_dur)) (itoa m_dur)) ")"))))
      (setq room_ent (entlast))
    )
  )
  (list (cons "success" T)
        (cons "equinox" eq_text)
        (cons "h_m" h)
        (cons "start" (mo:sun-fmt (min time1 time2)))
        (cons "end" (mo:sun-fmt (max time1 time2)))
        (cons "duration_min" total_min)
        (cons "duration" (strcat (itoa h_dur) ":" (if (< m_dur 10) (strcat "0" (itoa m_dur)) (itoa m_dur))))
        (cons "block" blk_name)
        (cons "insert_handle" ins)
        (cons "description_handle" desc_ent)
        (cons "room_label_handle" room_ent))
)

;;; =====================================================================================
;;; Vehicle swept path (from CAYENNE.LSP v2.2 / SCIEZKA): helpers copied with the prefix mosp:
;;; =====================================================================================
;;; Exact tractrix step per calculation step (tan(a/2) * exp(-D/L), as in QuickTurn2);
;;; envelope of the whole body with mirrors; outer tyre faces. Default vehicle: Porsche Cayenne E3
;;; (no rear-axle steering); values in mm, override with the "vehicle" option.

;;; ---------------- dane pojazdu [mm] ----------------
(setq *MOSP:VEH0*
  '((NAME . "Porsche Cayenne E3 (bez skretnej tylnej osi)")
    (L    . 2895.0)    ; rozstaw osi
    (FO   . 990.0)     ; zwis przedni  (os przednia -> zderzak przedni)
    (RO   . 1045.0)    ; zwis tylny    (os tylna -> zderzak tylny)
    (W    . 1983.0)    ; szerokosc nadwozia bez lusterek
    (WM   . 2194.0)    ; szerokosc z lusterkami
    (MX   . 1100.0)    ; poczatek lusterka ZA osia przednia (szacunek)
    (MD   . 230.0)     ; dlugosc lusterka wzdluz auta (szacunek)
    (TRF  . 1676.0)    ; rozstaw kol przednich (srodek opony - srodek opony)
    (TRR  . 1676.0)    ; rozstaw kol tylnych
    (TW   . 255.0)     ; szerokosc opony (255/55 R19)
    (TD   . 763.0)     ; srednica opony
    (TC   . 12100.0)   ; srednica zawracania (kraweznik-kraweznik)
   ))

(setq *MOSP:SIDE* 200.0) ; gestosc punktow na bokach karoserii [mm]
(setq *MOSP:ETAP* "")    ; aktualny etap - pokazywany przy bledzie
;;; ---------------- funkcje pomocnicze ----------------
(defun mosp:v (k) (cdr (assoc k *MOSP:VEH*)))
(defun mosp:d (k) (cdr (assoc k *MOSP:DEF*)))
(defun mosp:2d (p) (list (car p) (cadr p)))
(defun mosp:3d (p) (list (car p) (cadr p) 0.0))
(defun mosp:add (a b) (list (+ (car a) (car b)) (+ (cadr a) (cadr b))))
(defun mosp:sub (a b) (list (- (car a) (car b)) (- (cadr a) (cadr b))))
(defun mosp:mul (v k) (list (* (car v) k) (* (cadr v) k)))
(defun mosp:unit (v / dd)
  (setq dd (sqrt (+ (* (car v) (car v)) (* (cadr v) (cadr v)))))
  (if (> dd 1e-12) (list (/ (car v) dd) (/ (cadr v) dd)) (list 1.0 0.0))
)
(defun mosp:left (v) (list (- (cadr v)) (car v)))
(defun mosp:deg (a) (* a (/ 180.0 pi)))
(defun mosp:tanf (a) (/ (sin a) (cos a)))
(defun mosp:dang (a b / dd)
  (setq dd (- a b))
  (while (> dd pi) (setq dd (- dd (* 2.0 pi))))
  (while (<= dd (- pi)) (setq dd (+ dd (* 2.0 pi))))
  dd
)
(defun mosp:mid (a b) (mosp:mul (mosp:add a b) 0.5))
(defun mosp:ceil (x) (if (> x (fix x)) (1+ (fix x)) (fix x)))
(defun mosp:setnth (lst k v / i)
  (setq i -1)
  (mapcar (function (lambda (x) (setq i (1+ i)) (if (= i k) v x))) lst)
)

;;; minimalny promien prowadzonej sciezki [mm]: bk = T cofanie (tylna os)
(defun mosp:rmin_mm (bk / L TRf TCd rr)
  (setq L (mosp:v 'L) TRf (mosp:v 'TRF) TCd (mosp:v 'TC)
        rr (- (sqrt (- (* 0.25 TCd TCd) (* L L))) (* 0.5 TRf)))
  (if bk rr (sqrt (+ (* rr rr) (* L L))))
)
;;; ten sam promien w pelnych cm, zaokraglony w gore (do FILLET)
(defun mosp:rfil_cm (bk) (mosp:ceil (/ (mosp:rmin_mm bk) 10.0)))

;;; punkt q=(x y) z ukladu auta -> uklad rysunku
;;; (x do przodu od srodka osi przedniej, y w lewo)
(defun mosp:xf (P0 ux nl q)
  (mosp:add P0 (mosp:add (mosp:mul ux (car q)) (mosp:mul nl (cadr q))))
)

;;; punkt i styczna na krzywej w odleglosci dist od poczatku
(defun mosp:pt (crv dist / p)
  (setq p (vlax-curve-getPointAtDist crv dist))
  (if (null p) (setq p (vlax-curve-getEndPoint crv)))
  (mosp:2d p)
)
(defun mosp:tan (crv dist / p)
  (setq p (vlax-curve-getParamAtDist crv dist))
  (if (null p) (setq p (vlax-curve-getEndParam crv)))
  (mosp:unit (mosp:2d (vlax-curve-getFirstDeriv crv p)))
)

;;; rzut punktu na sciezke pomocnicza: (pikietaz  odsuniecie_ze_znakiem  punkt)
;;; odsuniecie > 0 = po lewej stronie kierunku jazdy
(defun mosp:proj (crv p / cp par st dv off)
  (setq cp (vlax-curve-getClosestPointTo crv (mosp:3d p)))
  (if cp (setq par (vlax-curve-getParamAtPoint crv cp)))
  (if par
    (progn
      (setq st  (vlax-curve-getDistAtParam crv par)
            dv  (vlax-curve-getFirstDeriv crv par)
            cp  (mosp:2d cp)
            off (distance cp p))
      (if (< (- (* (car dv) (- (cadr p) (cadr cp)))
                (* (cadr dv) (- (car p) (car cp))))
             0.0)
        (setq off (- off))
      )
      (list st off p)
    )
  )
)

;;; ---------------- automatyczne zaokraglanie polilinii ----------------
;;; odczyt wierzcholkow i wybrzuszen LWPOLYLINE (bez duplikatow)
(defun mosp:plread (e / pts bls)
  (foreach g (entget e)
    (cond
      ((= (car g) 10)
       (if (or (null pts) (> (distance (mosp:2d (cdr g)) (car pts)) 1e-9))
         (setq pts (cons (mosp:2d (cdr g)) pts) bls (cons 0.0 bls))))
      ((= (car g) 42) (setq bls (cons (cdr g) (cdr bls))))
    )
  )
  (list (reverse pts) (reverse bls))
)

(defun mosp:push (v lst)
  (if (and lst (< (distance (car v) (car (car lst))) 1e-9))
    (cons v (cdr lst))
    (cons v lst))
)

;;; Zaokragla narozniki miedzy odcinkami prostymi promieniem R.
;;; mk = T -> tworzy nowa polilinie na SP_SCIEZKA i znaczniki problemow,
;;; mk = nil -> tylko analiza.
;;; Zwraca: (nowa_polilinia  liczba_naroznikow  liczba_problemow  najmn_promien)
(defun mosp:fixpl (e R mk mrad / rd pts bls n i p1 p2 b ca h ln dS dE lens
                              a tl th sg f np nsh rfx u out)
  (setq rd (mosp:plread e) pts (car rd) bls (cadr rd) n (length pts) nsh 0 np 0)
  (if (< n 2)
    nil
    (progn
      (if mk (progn (mosp:layer "SP_SCIEZKA" 2) (mosp:layer "SP_PRZEKROCZENIE" 30)))
      ;; odcinki: kierunek na poczatku i koncu, dlugosc, ciasne luki
      (setq i 0)
      (while (< i (1- n))
        (setq p1 (nth i pts) p2 (nth (1+ i) pts) b (nth i bls)
              ca (angle p1 p2) ln (distance p1 p2) h (* 2.0 (atan b)))
        (setq dS (cons (- ca h) dS) dE (cons (+ ca h) dE) lens (cons ln lens))
        (if (and (/= b 0.0)
                 (< (/ (* ln (+ 1.0 (* b b))) (* 4.0 (abs b))) (* R 0.9999)))
          (progn (setq np (1+ np))
                 (if mk (mosp:circle (mosp:mid p1 p2) mrad "SP_PRZEKROCZENIE"))))
        (setq i (1+ i))
      )
      (setq dS (reverse dS) dE (reverse dE) lens (reverse lens))
      ;; narozniki: dlugosc stycznej tl = R * tan(kat/2)
      (setq tl (list 0.0) th (list 0.0) sg (list 0.0) i 1)
      (while (< i (1- n))
        (setq a (mosp:dang (nth i dS) (nth (1- i) dE)))
        (cond
          ((< (abs a) 1e-6)
           (setq tl (cons 0.0 tl) th (cons 0.0 th) sg (cons 0.0 sg)))
          ((and (= (nth (1- i) bls) 0.0) (= (nth i bls) 0.0))
           (setq nsh (1+ nsh)
                 tl  (cons (* R (mosp:tanf (/ (abs a) 2.0))) tl)
                 th  (cons (abs a) th)
                 sg  (cons (if (> a 0.0) 1.0 -1.0) sg)))
          (T   ; zalamanie przy luku - nie poprawiane automatycznie
           (if (> (abs a) 0.0087)
             (progn (setq np (1+ np))
                    (if mk (mosp:circle (nth i pts) mrad "SP_PRZEKROCZENIE"))))
           (setq tl (cons 0.0 tl) th (cons 0.0 th) sg (cons 0.0 sg)))
        )
        (setq i (1+ i))
      )
      (setq tl (reverse (cons 0.0 tl)) th (reverse (cons 0.0 th)) sg (reverse (cons 0.0 sg)))
      ;; czy luki mieszcza sie na odcinkach - jesli nie, zmniejsz (i oznacz)
      (setq f (mapcar (function (lambda (x) 1.0)) tl) i 0)
      (while (< i (1- n))
        (setq a (+ (nth i tl) (nth (1+ i) tl)))
        (if (> a (nth i lens))
          (setq f (mosp:setnth f i (min (nth i f) (/ (nth i lens) a)))
                f (mosp:setnth f (1+ i) (min (nth (1+ i) f) (/ (nth i lens) a)))))
        (setq i (1+ i))
      )
      (setq i 0)
      (while (< i n)
        (if (and (< (nth i f) 0.9999) (> (nth i th) 0.0))
          (progn
            (setq tl (mosp:setnth tl i (* (nth i tl) (nth i f)))
                  np (1+ np)
                  a  (/ (nth i tl) (mosp:tanf (/ (nth i th) 2.0)))
                  rfx (if rfx (min rfx a) a))
            (if mk (mosp:circle (nth i pts) mrad "SP_PRZEKROCZENIE"))))
        (setq i (1+ i))
      )
      ;; budowa nowej polilinii
      (if mk
        (progn
          (setq out nil i 0)
          (while (< i (1- n))
            (setq p1 (nth i pts) p2 (nth (1+ i) pts) u (mosp:unit (mosp:sub p2 p1)))
            (setq out (mosp:push (cons (if (> (nth i tl) 0.0) (mosp:add p1 (mosp:mul u (nth i tl))) p1)
                                     (nth i bls))
                               out))
            (if (> (nth (1+ i) tl) 0.0)
              (setq out (mosp:push (cons (mosp:sub p2 (mosp:mul u (nth (1+ i) tl)))
                                       (* (nth (1+ i) sg) (mosp:tanf (/ (nth (1+ i) th) 4.0))))
                                 out)))
            (setq i (1+ i))
          )
          (setq out (reverse (mosp:push (cons (last pts) 0.0) out)))
          (entmake
            (append
              (list '(0 . "LWPOLYLINE") '(100 . "AcDbEntity") (cons 8 "SP_SCIEZKA")
                    '(100 . "AcDbPolyline") (cons 90 (length out)) '(70 . 0))
              (apply 'append
                     (mapcar (function (lambda (v) (list (cons 10 (car v)) (cons 42 (cdr v)))))
                             out))
            )
          )
          (list (entlast) nsh np rfx)
        )
        (list nil nsh np rfx)
      )
    )
  )
)

;;; ---------------- tworzenie obiektow ----------------
(setq mosp:*show* T mosp:*shown* nil mosp:*hidden* nil)

;;; creates the layer if missing; an existing layer that is off or frozen is switched on and thawed
;;; when mosp:*show* is T (otherwise only reported), so the result is never drawn invisibly (FINDINGS #52)
(defun mosp:layer (nm col / d c f)
  (if (not (tblsearch "LAYER" nm))
    (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord")
                   '(100 . "AcDbLayerTableRecord") (cons 2 nm) '(70 . 0)
                   (cons 62 col) '(6 . "Continuous")))
    (progn
      (setq d (entget (tblobjname "LAYER" nm))
            c (cdr (assoc 62 d))
            f (cdr (assoc 70 d)))
      (if (or (< c 0) (= 1 (logand 1 f)))
        (if mosp:*show*
          (progn
            (setq d (subst (cons 62 (abs c)) (assoc 62 d) d)
                  d (subst (cons 70 (logand f (~ 1))) (assoc 70 d) d))
            (entmod d)
            (if (not (member nm mosp:*shown*)) (setq mosp:*shown* (cons nm mosp:*shown*))))
          (if (not (member nm mosp:*hidden*)) (setq mosp:*hidden* (cons nm mosp:*hidden*)))))
    )
  )
  nm
)

(defun mosp:layers-begin (opts)
  (setq mosp:*show* (zwmcp:opt "show_layers" opts T) mosp:*shown* nil mosp:*hidden* nil))

(defun mosp:layers-end ()
  (if mosp:*shown* (vla-Regen (vla-get-ActiveDocument (vlax-get-acad-object)) acActiveViewport))
  (list (cons "layers_switched_on" (reverse mosp:*shown*)) (cons "hidden_layers" (reverse mosp:*hidden*))))
(defun mosp:pline (pts lay cl)
  (if (> (length pts) 1)
    (entmake
      (append
        (list '(0 . "LWPOLYLINE") '(100 . "AcDbEntity") (cons 8 lay)
              '(100 . "AcDbPolyline") (cons 90 (length pts)) (cons 70 (if cl 1 0)))
        (mapcar (function (lambda (p) (cons 10 p))) pts)
      )
    )
  )
)
(defun mosp:line (a b lay)
  (entmake (list '(0 . "LINE") (cons 8 lay) (cons 10 (mosp:3d a)) (cons 11 (mosp:3d b))))
)
(defun mosp:circle (c rad lay)
  (entmake (list '(0 . "CIRCLE") (cons 8 lay) (cons 10 (mosp:3d c)) (cons 40 rad)))
)

;;; obrys karoserii z lusterkami w ukladzie auta (zmienne v.. z c:CAYENNE)
(defun mosp:bodyloc (/ hw hm xm1 xm2 xr)
  (setq hw  (/ vW 2.0)
        hm  (/ vWM 2.0)
        xm1 (- vMX)
        xm2 (- (+ vMX vMD))
        xr  (- (+ vL vRO)))
  (list (list vFO hw) (list vFO (- hw))
        (list xm1 (- hw)) (list xm1 (- hm)) (list xm2 (- hm)) (list xm2 (- hw))
        (list xr (- hw)) (list xr hw)
        (list xm2 hw) (list xm2 hm) (list xm1 hm) (list xm1 hw))
)

;;; punkty do liczenia obwiedni: wierzcholki obrysu + zageszczone boki
(defun mosp:envloc (/ hw xr x lst)
  (setq hw (/ vW 2.0) xr (- (+ vL vRO)) lst (mosp:bodyloc) x vFO)
  (while (> x xr)
    (setq lst (cons (list x hw) (cons (list x (- hw)) lst))
          x   (- x vSS))
  )
  ;; punkt na wysokosci osi tylnej (krawedz wewnetrzna w luku)
  (cons (list (- vL) hw) (cons (list (- vL) (- hw)) lst))
)

;;; prostokat opony: c - srodek, dr - kierunek opony
(defun mosp:tyre (c dr lay / n da nb)
  (setq n  (mosp:left dr)
        da (mosp:mul dr (/ vTD 2.0))
        nb (mosp:mul n (/ vTW 2.0)))
  (mosp:pline (list (mosp:add c (mosp:add da nb)) (mosp:add c (mosp:sub da nb))
                  (mosp:sub c (mosp:add da nb)) (mosp:sub c (mosp:sub da nb)))
            lay T)
)

;;; obrys auta w pozycji: PF - srodek osi przedniej, PR - srodek osi tylnej,
;;; ux - kierunek auta, tg - kierunek kol przednich
(defun mosp:car (PF PR ux tg lay / nl)
  (setq nl (mosp:left ux))
  (mosp:pline (mapcar (function (lambda (q) (mosp:xf PF ux nl q))) (mosp:bodyloc)) lay T)
  (mosp:tyre (mosp:add PF (mosp:mul nl (/ vTRF 2.0))) tg lay)
  (mosp:tyre (mosp:sub PF (mosp:mul nl (/ vTRF 2.0))) tg lay)
  (mosp:tyre (mosp:add PR (mosp:mul nl (/ vTRR 2.0))) ux lay)
  (mosp:tyre (mosp:sub PR (mosp:mul nl (/ vTRR 2.0))) ux lay)
)

;;; ---------------- swept path: non-interactive entry points ----------------
;;; opts (assoc list):
;;;   "mode" "Przod" (forward, path = centre of the FRONT axle) / "Tyl" (reversing, path = REAR axle)
;;;   "units" 1 = mm, 2 = cm, 3 = m | "reverse" T = drive against the drawing direction of the path
;;;   "step" calculation step [mm] | "interval" draw the car outline every [mm], 0 = none
;;;   "auto_fillet" T = round sharp LWPOLYLINE corners first (new polyline on SP_SCIEZKA)
;;;   "vehicle" assoc list overriding *MOSP:VEH0* values in mm, e.g. (("L" . 2900.0) ("TC" . 11800.0))

(defun mosp:vehicle (overrides / veh sym)
  (setq veh *MOSP:VEH0*)
  (foreach o overrides
    (setq sym (read (car o)))
    (if (assoc sym veh)
      (setq veh (subst (cons sym (if (numberp (cdr o)) (float (cdr o)) (cdr o))) (assoc sym veh) veh))
      (zwmcp:fail (strcat "Unknown vehicle parameter: " (car o)))
    )
  )
  veh
)

(defun mosp:scale (un) (cond ((= un 2) 0.1) ((= un 3) 0.001) (T 1.0)))

(defun mo:round-path (path-h opts / *MOSP:VEH* bk sc ent typ rcm fx)
  (mosp:layers-begin opts)
  (setq *MOSP:VEH* (mosp:vehicle (zwmcp:opt "vehicle" opts nil))
        bk (= (zwmcp:opt "mode" opts "Przod") "Tyl")
        sc (mosp:scale (zwmcp:opt "units" opts 2))
        ent (zwmcp:ent path-h)
        typ (cdr (assoc 0 (entget ent))))
  (if (/= typ "LWPOLYLINE")
    (zwmcp:fail (strcat "Path " path-h " is " typ "; rounding works on a 2D polyline (LWPOLYLINE).")))
  (setq rcm (float (zwmcp:opt "radius_cm" opts (mosp:rfil_cm bk))))
  (setq fx (mosp:fixpl ent (* rcm 10.0 sc) T (* 400.0 sc)))
  (if (null fx) (zwmcp:fail "Polyline has fewer than 2 vertices."))
  (list (cons "success" T)
        (cons "new_path_handle" (car fx))
        (cons "corners_rounded" (cadr fx))
        (cons "problems" (caddr fx))
        (cons "smallest_radius_cm" (if (cadddr fx) (/ (cadddr fx) sc 10.0) nil))
        (cons "radius_cm" rcm)
        (cons "min_radius_cm" (mosp:rfil_cm bk))
        (cons "layer" "SP_SCIEZKA")
        (cons "problem_layer" "SP_PRZEKROCZENIE")
        (cons "layers" (mosp:layers-end)))
)

(defun mo:swept-path (path-h opts / *MOSP:VEH* ent typ bk un sc rev stp intv tot slist s sd
                        vL vFO vRO vW vWM vMX vMD vTRF vTRR vTW vTD vTC vSS rr dmax rfmin rmin
                        PF PFp PR ux tg tg0 tgN nl phi dd th a0 a1 dl dmaxf over prevover nover nextd hb
                        lPOSE lPF lTFL lTFR lTRL lTRR ext pth envq lENV rec k bidx rmax rminr lLeft lRight
                        ps q p npts GP tgw dlt thp sprev rfc fx r env tyres pos warn PFe PRe sdl psdl djmax)
  (mosp:layers-begin opts)
  (setq *MOSP:VEH* (mosp:vehicle (zwmcp:opt "vehicle" opts nil))
        bk (= (zwmcp:opt "mode" opts "Przod") "Tyl")
        ent (zwmcp:ent path-h)
        typ (cdr (assoc 0 (entget ent))))
  (if (member typ '("INSERT" "REGION" "HATCH" "TEXT" "MTEXT" "DIMENSION"))
    (zwmcp:fail (strcat "Path " path-h " is " typ ", not a path (use a polyline, line, arc or spline).")))
  (if (vl-catch-all-error-p (vl-catch-all-apply 'vlax-curve-getEndParam (list ent)))
    (zwmcp:fail (strcat "Path " path-h " (" typ ") is not a curve that can be followed.")))
  (setq un (zwmcp:opt "units" opts 2)
        sc (mosp:scale un)
        rev (zwmcp:opt "reverse" opts nil)
        stp (* sc (float (zwmcp:opt "step" opts 50.0)))
        intv (* sc (float (zwmcp:opt "interval" opts 0.0))))
  (if (<= stp 0.0) (zwmcp:fail "step must be > 0."))

  (setq vL   (* sc (mosp:v 'L))   vFO  (* sc (mosp:v 'FO))  vRO (* sc (mosp:v 'RO))
        vW   (* sc (mosp:v 'W))   vWM  (* sc (mosp:v 'WM))  vMX (* sc (mosp:v 'MX))
        vMD  (* sc (mosp:v 'MD))  vTRF (* sc (mosp:v 'TRF)) vTRR (* sc (mosp:v 'TRR))
        vTW  (* sc (mosp:v 'TW))  vTD  (* sc (mosp:v 'TD))  vTC (* sc (mosp:v 'TC))
        vSS  (* sc *MOSP:SIDE*)
        hb   (/ vTW 2.0))
  (if (<= (* 0.5 vTC) vL)
    (zwmcp:fail "Vehicle data: turning circle TC must be larger than 2 x wheelbase L."))
  (setq rr    (- (sqrt (- (* 0.25 vTC vTC) (* vL vL))) (* 0.5 vTRF))
        dmax  (atan vL rr)
        rfmin (sqrt (+ (* rr rr) (* vL vL)))
        rmin  (if bk rr rfmin))

  ;; optional automatic rounding of sharp polyline corners
  (if (and (= typ "LWPOLYLINE") (zwmcp:opt "auto_fillet" opts T))
    (progn
      (setq rfc (mosp:rfil_cm bk)
            fx  (mosp:fixpl ent (* rfc 10.0 sc) nil 0.0))
      (if (and fx (> (+ (cadr fx) (caddr fx)) 0))
        (progn
          (setq fx (mosp:fixpl ent (* rfc 10.0 sc) T (* 400.0 sc)))
          (if (car fx) (setq ent (car fx)))
        )
        (setq fx nil)
      )
    )
  )

  (setq tot (vlax-curve-getDistAtParam ent (vlax-curve-getEndParam ent)))
  (if (or (null tot) (<= tot 1e-6)) (zwmcp:fail "The path has zero length."))
  (cond
    ((< tot vL) (setq warn "Path is shorter than the wheelbase; result is not meaningful (check drawing units)."))
    ((> tot (* 200.0 vL)) (setq warn "Path is very long for a manoeuvre; check drawing units."))
  )

  (mosp:layer "SP_OBWIEDNIA" 7)
  (mosp:layer "SP_OPONY" 7)
  (if (> intv 0.0) (mosp:layer "SP_OBRYS" 8))

  (setq mosp:*tmp* nil)
  (setq r (vl-catch-all-apply 'mosp:simulate nil))
  (if (and mosp:*tmp* (entget mosp:*tmp*)) (entdel mosp:*tmp*))
  (setq mosp:*tmp* nil)
  (if (vl-catch-all-error-p r)
    (zwmcp:fail (strcat "Swept path calculation failed: "
                        (if zwmcp:*last-error* zwmcp:*last-error* (vl-catch-all-error-message r)))))

  (list (cons "success" T)
        (cons "vehicle" (mosp:v 'NAME))
        (cons "mode" (if bk "reverse (path = rear axle centre)" "forward (path = front axle centre)"))
        (cons "path_handle" ent)
        (cons "fillet" (if fx (list (cons "new_path_handle" (car fx)) (cons "corners_rounded" (cadr fx))
                                    (cons "problems" (caddr fx)) (cons "radius_cm" rfc))
                         nil))
        (cons "max_steer_deg" (mosp:deg dmax))
        (cons "required_steer_deg" (mosp:deg dmaxf))
        (cons "max_steer_change_per_step_deg" (mosp:deg djmax))
        (cons "steering_continuous" (if (< (mosp:deg djmax) 3.0) T 'false))
        (cons "min_path_radius_mm" (/ rmin sc))
        (cons "fillet_radius_min_cm" (mosp:ceil (/ rmin sc 10.0)))
        (cons "feasible" (if (> nover 0) 'false T))
        (cons "overshoot_places" nover)
        (cons "envelope_handle" env)
        (cons "tyre_handles" tyres)
        (cons "end_position" (list (cons "front_axle" PFe) (cons "rear_axle" PRe)))
        (cons "position_handles" pos)
        (cons "layers" (mosp:layers-end))
        (cons "warning" warn))
)

;; body of the simulation (dynamic scope: uses the locals of mo:swept-path)
(defun mosp:simulate ( / )
  (setq slist nil k 0)
  (while (< (* k stp) tot)
    (setq slist (cons (* k stp) slist) k (1+ k)))
  (setq slist (reverse (cons tot slist)))

  (setq phi nil PFp nil thp nil sprev nil nextd 0.0 dmaxf 0.0 nover 0 prevover nil psdl nil djmax 0.0)
  (foreach s slist
    (setq sd (if rev (- tot s) s)
          GP (mosp:pt ent sd)
          tg (mosp:tan ent sd))
    (if rev (setq tg (mosp:mul tg -1.0)))
    (if (null tg0) (setq tg0 tg))
    (if bk
      ;; reversing: rear axle centre follows the path, car axis tangent to the path
      (setq th  (angle '(0.0 0.0) tg)
            dlt (if (and thp (> (- s sprev) 1e-9))
                  (atan (* (- vL) (/ (mosp:dang th thp) (- s sprev))))
                  0.0)
            phi (+ th pi)
            ux  (list (cos phi) (sin phi))
            PR  GP
            PF  (mosp:add PR (mosp:mul ux vL))
            tgw (list (cos (+ phi dlt)) (sin (+ phi dlt)))
            dl  (abs dlt)
            sdl dlt
            thp th
            sprev s)
      ;; forward: front axle centre follows the path, rear axle on the exact tractrix step
      (progn
        (if (null phi)
          (setq phi (angle '(0.0 0.0) tg))
          (progn
            (setq dd (distance PFp GP))
            (if (> dd 1e-9)
              (setq th  (angle PFp GP)
                    a0  (mosp:dang phi th)
                    a1  (if (< (abs a0) 1e-12)
                          0.0
                          (* (if (< a0 0.0) -1.0 1.0) 2.0
                             (atan (* (mosp:tanf (/ (abs a0) 2.0)) (exp (/ (- dd) vL))))))
                    phi (+ th a1))
            )
          )
        )
        (setq ux  (list (cos phi) (sin phi))
              PF  GP
              PR  (mosp:sub PF (mosp:mul ux vL))
              PFp GP
              tgw tg
              dl  (abs (mosp:dang (angle '(0.0 0.0) tg) phi))
              sdl (mosp:dang (angle '(0.0 0.0) tg) phi))
      )
    )
    (setq nl (mosp:left ux) tgN tg)
    ;; steering change between calculation steps: a jump means the path has a kink (forward) or a curvature
    ;; jump (reverse) - the wheels would have to turn instantly (FINDINGS #57)
    (if psdl (setq djmax (max djmax (abs (- sdl psdl)))))
    (setq psdl sdl)
    (setq dmaxf (max dmaxf dl)
          over  (> dl (+ dmax 1e-4)))
    (setq lPOSE (cons (list PF ux) lPOSE))
    (if (or (null lPF) (> (distance GP (car lPF)) (* 0.01 stp)))
      (setq lPF (cons GP lPF)))
    (setq lTFL (cons (mosp:add (mosp:add PF (mosp:mul nl (/ vTRF 2.0))) (mosp:mul (mosp:left tgw) hb)) lTFL)
          lTFR (cons (mosp:sub (mosp:sub PF (mosp:mul nl (/ vTRF 2.0))) (mosp:mul (mosp:left tgw) hb)) lTFR)
          lTRL (cons (mosp:add PR (mosp:mul nl (+ (/ vTRR 2.0) hb))) lTRL)
          lTRR (cons (mosp:sub PR (mosp:mul nl (+ (/ vTRR 2.0) hb))) lTRR))
    (cond
      ((and over (not prevover))
       (setq nover (1+ nover))
       (mosp:layer "SP_PRZEKROCZENIE" 30)
       (mosp:circle PF (* 400.0 sc) "SP_PRZEKROCZENIE")
       (mosp:car PF PR ux tgw "SP_PRZEKROCZENIE"))
      ((and (> intv 0.0) (or (>= s nextd) (= s tot)))
       (mosp:car PF PR ux tgw "SP_OBRYS"))
    )
    (while (and (> intv 0.0) (>= s nextd)) (setq nextd (+ nextd intv)))
    (setq prevover over)
  )
  ;; end pose: kept apart because the envelope loop below reuses PF / ux / nl
  ;; (CAYENNE.LSP v2.2 drew SP_POZYCJA with the front axle of the FIRST pose)
  (setq PFe PF PRe PR)

  ;; envelope of the body with mirrors: helper path extended at both ends, project all body points
  (setq ext (+ vL vRO vFO vW (* 2000.0 sc))
        lPF (reverse lPF)
        pth (append (list (mosp:sub (car lPF) (mosp:mul tg0 ext)))
                    lPF
                    (list (mosp:add (last lPF) (mosp:mul tgN ext)))))
  (mosp:pline pth "0" nil)
  (setq mosp:*tmp* (entlast))
  (setq envq (mosp:envloc) lENV nil npts 0)
  (foreach ps lPOSE
    (setq PF (car ps) ux (cadr ps) nl (mosp:left ux))
    (foreach q envq
      (setq p (mosp:xf PF ux nl q))
      (if (setq rec (mosp:proj mosp:*tmp* p))
        (setq lENV (cons rec lENV) npts (1+ npts)))
    )
  )
  (entdel mosp:*tmp*)
  (setq mosp:*tmp* nil)
  (setq lENV (vl-sort lENV (function (lambda (a b) (< (car a) (car b))))))
  (setq bidx nil lLeft nil lRight nil)
  (foreach rec lENV
    (setq k (fix (/ (car rec) stp)))
    (if (/= k bidx)
      (progn
        (if bidx (setq lLeft  (cons (caddr rmax) lLeft)
                       lRight (cons (caddr rminr) lRight)))
        (setq bidx k rmax rec rminr rec))
      (progn
        (if (> (cadr rec) (cadr rmax)) (setq rmax rec))
        (if (< (cadr rec) (cadr rminr)) (setq rminr rec)))
    )
  )
  (if bidx (setq lLeft  (cons (caddr rmax) lLeft)
                 lRight (cons (caddr rminr) lRight)))

  (mosp:pline (append (reverse lLeft) lRight) "SP_OBWIEDNIA" T)
  (setq env (entlast) tyres nil)
  (foreach p (list lTFL lTFR lTRL lTRR)
    (mosp:pline (reverse p) "SP_OPONY" nil)
    (setq tyres (cons (entlast) tyres)))
  (setq tyres (reverse tyres))

  (mosp:layer "SP_POZYCJA" 3)
  (mosp:line PRe PFe "SP_POZYCJA")
  (setq pos (list (entlast)))
  (mosp:circle PFe (* 150.0 sc) "SP_POZYCJA")
  (setq pos (cons (entlast) pos))
  (mosp:circle PRe (* 150.0 sc) "SP_POZYCJA")
  (setq pos (reverse (cons (entlast) pos)))
  T
)

;;; ---------------- vehicle route (plan_vehicle_route / draw_vehicle_route) ----------------
;;; geometry of a LWPOLYLINE or LINE: type, layer, closed flag, vertices (x y bulge)
(defun mo:curve-data (h / e d typ vs)
  (setq e (zwmcp:ent h) d (entget e) typ (cdr (assoc 0 d)) vs nil)
  (cond
    ((= typ "LWPOLYLINE")
     (foreach g d
       (cond ((= (car g) 10) (setq vs (cons (list (cadr g) (caddr g) 0.0) vs)))
             ((= (car g) 42) (setq vs (cons (list (car (car vs)) (cadr (car vs)) (cdr g)) (cdr vs))))))
     (setq vs (reverse vs)))
    ((= typ "LINE")
     (setq vs (list (list (cadr (assoc 10 d)) (caddr (assoc 10 d)) 0.0)
                    (list (cadr (assoc 11 d)) (caddr (assoc 11 d)) 0.0))))
    (T (zwmcp:fail (strcat "Entity " h " is " typ "; use a LWPOLYLINE (boundary, vector) or a LINE (vector)."))))
  (list (cons "handle" h) (cons "type" typ) (cons "layer" (cdr (assoc 8 d)))
        (cons "closed" (if (and (= typ "LWPOLYLINE") (= 1 (logand 1 (cdr (assoc 70 d))))) T 'false))
        (cons "vertices" vs)
        (cons "insunits" (getvar "INSUNITS")))
)

;;; draws one LWPOLYLINE (with bulges) per segment; forward = green, reverse = red
(defun mo:route-draw (lay segs / out vs gear)
  (if (not (tblsearch "LAYER" lay))
    (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord") '(100 . "AcDbLayerTableRecord")
                   (cons 2 lay) '(70 . 0) '(62 . 4) '(6 . "Continuous"))))
  (foreach seg segs
    (setq vs (cdr (assoc "vertices" seg)) gear (cdr (assoc "gear" seg)))
    (entmake (append
               (list '(0 . "LWPOLYLINE") '(100 . "AcDbEntity") (cons 8 lay) (cons 62 (if (= gear "reverse") 1 3))
                     '(100 . "AcDbPolyline") (cons 90 (length vs)) '(70 . 0))
               (apply 'append (mapcar '(lambda (v) (list (list 10 (car v) (cadr v)) (cons 42 (caddr v)))) vs))))
    (setq out (cons (list (cons "gear" gear) (cons "handle" (cdr (assoc 5 (entget (entlast)))))) out)))
  (reverse out)
)

;;; envelope polylines against the boundary: intersections and smallest vertex distance
(defun mo:envelope-check (boundary-h env-hs / lim out env ip n e dmin p d)
  (setq lim (vlax-ename->vla-object (zwmcp:ent boundary-h)))
  (foreach h env-hs
    (setq env (vlax-ename->vla-object (zwmcp:ent h))
          ip  (vlax-variant-value (vla-IntersectWith env lim acExtendNone))
          n   (if (> (vlax-safearray-get-u-bound ip 1) 0) (/ (1+ (vlax-safearray-get-u-bound ip 1)) 3) 0)
          e   (zwmcp:ent h) dmin 1e99)
    (foreach g (entget e)
      (if (= (car g) 10)
        (setq p (list (cadr g) (caddr g) 0.0)
              d (distance p (vlax-curve-getClosestPointTo lim p))
              dmin (min d dmin))))
    (setq out (cons (list (cons "envelope_handle" h) (cons "intersections" n) (cons "min_distance" dmin)) out)))
  (reverse out)
)

;;; boundary parts the route has to violate: thick red polyline on the edge part + label
;;; marks: list of (("from" x y) ("to" x y) ("text" . "...")) ; width / txth in drawing units
(defun mo:mark-violations (lay marks width txth / out p1 p2 mid ang)
  (if (not (tblsearch "LAYER" lay))
    (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord") '(100 . "AcDbLayerTableRecord")
                   (cons 2 lay) '(70 . 0) '(62 . 1) '(6 . "Continuous"))))
  (foreach m marks
    (setq p1 (cdr (assoc "from" m)) p2 (cdr (assoc "to" m)))
    (if (< (distance p1 p2) (* 2.0 width))      ; a point-like contact: show at least a short dash
      (setq ang (angle p1 p2)
            p1 (polar p1 (+ ang pi) width) p2 (polar p2 ang width)))
    (setq mid (mapcar '(lambda (a b) (/ (+ a b) 2.0)) p1 p2)
          ang (angle p1 p2))
    (entmake (list '(0 . "LWPOLYLINE") '(100 . "AcDbEntity") (cons 8 lay) '(62 . 1)
                   '(100 . "AcDbPolyline") '(90 . 2) '(70 . 0) (cons 43 width)
                   (list 10 (car p1) (cadr p1)) (list 10 (car p2) (cadr p2))))
    (setq out (cons (cdr (assoc 5 (entget (entlast)))) out))
    (if (< (cos ang) -1e-9) (setq ang (+ ang pi)))  ; keep the label readable
    (entmake (list '(0 . "TEXT") '(100 . "AcDbEntity") (cons 8 lay) '(62 . 1) '(100 . "AcDbText")
                   (list 10 (car mid) (cadr mid) 0.0) (cons 40 txth) (cons 1 (cdr (assoc "text" m)))
                   (cons 50 ang) '(72 . 1) (list 11 (car mid) (+ (cadr mid) (* 0.6 txth)) 0.0) '(100 . "AcDbText") '(73 . 0)))
    (setq out (cons (cdr (assoc 5 (entget (entlast)))) out)))
  (reverse out)
)
