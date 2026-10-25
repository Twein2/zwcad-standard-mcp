;;; zwmcp_core.lsp -- AutoLISP side of the zwcad-standard-mcp LISP channel (MIT licence)
;;;
;;; Loaded into each document on demand by the MCP server (read + eval of a comment-free copy, so
;;; SECURELOAD / trusted paths are not involved). Keep this file ASCII-only.
;;;
;;; Entry point: (zwmcp:run "<ipc dir>/" "<request id>" <undo T/nil>)
;;;   reads req_<id>.lsp (one expression), evaluates it inside vl-catch-all-apply and writes
;;;   res_<id>.json = {"id":..., "unicode":bool, "ok":true, "value":<json>} or {"ok":false, "error":"..."}
;;;
;;; LISP -> JSON mapping (zwmcp:json):
;;;   nil -> null, T -> true, the symbol FALSE -> false, INT/REAL -> number, STR -> string,
;;;   ENAME / VLA-OBJECT -> handle string, assoc list with string keys -> object, other lists -> array,
;;;   dotted pair (a . b) -> [a, b].

(vl-load-com)

;; Unicode LISP (code points above 255 survive chr/ascii)? Then non-ASCII chars are written as \uXXXX.
(setq zwmcp:*unicode*
  (= (vl-catch-all-apply 'ascii (list (vl-catch-all-apply 'chr (list 322)))) 322))

(defun zwmcp:fail (msg)
  (setq zwmcp:*last-error* msg)
  (exit)
)

(defun zwmcp:hex4 (n / s d)
  (setq s "")
  (repeat 4
    (setq d (rem n 16)
          s (strcat (substr "0123456789ABCDEF" (1+ d) 1) s)
          n (/ n 16))
  )
  s
)

;; Concatenate a list of strings in chunks (keeps apply's argument list short).
(defun zwmcp:join (lst / acc chunk i)
  (setq acc "")
  (while lst
    (setq chunk nil i 0)
    (while (and lst (< i 200))
      (setq chunk (cons (car lst) chunk) lst (cdr lst) i (1+ i))
    )
    (setq acc (strcat acc (apply 'strcat (reverse chunk))))
  )
  acc
)

(defun zwmcp:joinsep (lst sep / out)
  (foreach s lst
    (setq out (if out (cons s (cons sep out)) (list s)))
  )
  (zwmcp:join (reverse out))
)

(defun zwmcp:jstr (s / out)
  (foreach n (vl-string->list s)
    (setq out
      (cons
        (cond
          ((= n 34) "\\\"")
          ((= n 92) "\\\\")
          ((= n 10) "\\n")
          ((= n 13) "\\r")
          ((= n 9) "\\t")
          ((< n 32) (strcat "\\u" (zwmcp:hex4 n)))
          ((and zwmcp:*unicode* (> n 126)) (strcat "\\u" (zwmcp:hex4 n)))
          (T (chr n))
        )
        out
      )
    )
  )
  (strcat "\"" (zwmcp:join (reverse out)) "\"")
)

(defun zwmcp:jnum (x / s)
  (if (= (type x) 'INT)
    (itoa x)
    (progn
      (setq s (vl-string-translate "," "." (rtos x 2 10)))
      (cond
        ((= (substr s 1 1) ".") (setq s (strcat "0" s)))
        ((= (substr s 1 2) "-.") (setq s (strcat "-0" (substr s 2))))
      )
      (if (= (substr s (strlen s)) ".") (setq s (strcat s "0")))
      s
    )
  )
)

(defun zwmcp:properp (v)
  (while (and v (listp v)) (setq v (cdr v)))
  (null v)
)

(defun zwmcp:objp (v / ok)
  (if (and v (listp v) (zwmcp:properp v))
    (progn
      (setq ok T)
      (foreach e v
        (if (not (and e (listp e) (= (type (car e)) 'STR))) (setq ok nil))
      )
    )
  )
  ok
)

(defun zwmcp:json (v / ty)
  (setq ty (type v))
  (cond
    ((null v) "null")
    ((eq v T) "true")
    ((eq v 'false) "false")
    ((eq v :vlax-true) "true")
    ((eq v :vlax-false) "false")
    ((= ty 'VARIANT) (zwmcp:json (vlax-variant-value v)))
    ((= ty 'SAFEARRAY)
     (zwmcp:json (if (>= (vlax-safearray-get-u-bound v 1) 0) (vlax-safearray->list v) nil)))
    ((= ty 'INT) (itoa v))
    ((= ty 'REAL) (zwmcp:jnum v))
    ((= ty 'STR) (zwmcp:jstr v))
    ((= ty 'ENAME) (zwmcp:jstr (cdr (assoc 5 (entget v)))))
    ((= ty 'VLA-OBJECT) (zwmcp:jstr (vla-get-Handle v)))
    ((= ty 'SYM) (zwmcp:jstr (vl-symbol-name v)))
    ((= ty 'LIST)
     (cond
       ((not (zwmcp:properp v))
        (strcat "[" (zwmcp:json (car v)) "," (zwmcp:json (cdr v)) "]"))
       ((zwmcp:objp v)
        (strcat "{"
                (zwmcp:joinsep
                  (mapcar '(lambda (e) (strcat (zwmcp:jstr (car e)) ":" (zwmcp:json (cdr e)))) v)
                  ",")
                "}"))
       (T (strcat "[" (zwmcp:joinsep (mapcar 'zwmcp:json v) ",") "]"))
     ))
    (T (zwmcp:jstr (vl-princ-to-string v)))
  )
)

(defun zwmcp:read-file (path / f l s)
  (if (not (setq f (open path "r")))
    (zwmcp:fail (strcat "Cannot open request file: " path))
  )
  (setq s "")
  (while (setq l (read-line f))
    (setq s (strcat s l "\n"))
  )
  (close f)
  s
)

(defun zwmcp:eval-request (path)
  (eval (read (zwmcp:read-file path)))
)

(defun zwmcp:write-file (path text / f i n)
  (if (not (setq f (open path "w")))
    (progn (princ (strcat "\nzwcad-mcp: cannot write " path)) nil)
    (progn
      (setq i 1 n (strlen text))
      (while (<= i n)
        (princ (substr text i 2000) f)
        (setq i (+ i 2000))
      )
      (close f)
      T
    )
  )
)

;; Locals carry the zwmcp:% prefix on purpose: AutoLISP is dynamically scoped, so a top-level
;; (setq res ...) in execute_lisp code used to overwrite the runner's result path (FINDINGS #50).
(defun zwmcp:run (zwmcp:%dir zwmcp:%id zwmcp:%undo / zwmcp:%req zwmcp:%res zwmcp:%tmp zwmcp:%r zwmcp:%doc zwmcp:%body)
  (setq zwmcp:%req (strcat zwmcp:%dir "req_" zwmcp:%id ".lsp")
        zwmcp:%res (strcat zwmcp:%dir "res_" zwmcp:%id ".json")
        zwmcp:%tmp (strcat zwmcp:%dir "res_" zwmcp:%id ".tmp")
        zwmcp:*last-error* nil)
  ;; acknowledge: tells the server that the request started (long tools keep running after it)
  (zwmcp:write-file (strcat zwmcp:%dir "ack_" zwmcp:%id ".txt") "1")
  (if zwmcp:%undo
    (progn
      (setq zwmcp:%doc (vla-get-ActiveDocument (vlax-get-acad-object)))
      (vla-StartUndoMark zwmcp:%doc)
    )
  )
  (setq zwmcp:%r (vl-catch-all-apply 'zwmcp:eval-request (list zwmcp:%req)))
  (if zwmcp:%doc (vla-EndUndoMark zwmcp:%doc))
  (setq zwmcp:%body
    (if (vl-catch-all-error-p zwmcp:%r)
      (strcat "\"ok\":false,\"error\":"
              (zwmcp:jstr (if zwmcp:*last-error* zwmcp:*last-error* (vl-catch-all-error-message zwmcp:%r))))
      (progn
        (setq zwmcp:%r (vl-catch-all-apply 'zwmcp:json (list zwmcp:%r)))
        (if (vl-catch-all-error-p zwmcp:%r)
          (strcat "\"ok\":false,\"error\":"
                  (zwmcp:jstr (strcat "Result serialization failed: " (vl-catch-all-error-message zwmcp:%r))))
          (strcat "\"ok\":true,\"value\":" zwmcp:%r)
        )
      )
    )
  )
  (zwmcp:write-file zwmcp:%tmp
    (strcat "{\"id\":" (zwmcp:jstr zwmcp:%id) ",\"unicode\":" (if zwmcp:*unicode* "true" "false") "," zwmcp:%body "}"))
  (vl-file-delete zwmcp:%req)
  (vl-file-delete (strcat zwmcp:%dir "ack_" zwmcp:%id ".txt"))
  (vl-file-delete zwmcp:%res)
  (vl-file-rename zwmcp:%tmp zwmcp:%res)
  (princ)
)

;; ------------------------------------------------------------------ helpers used by the server

(defun zwmcp:ent (handle / e)
  (if (not (setq e (handent handle)))
    (zwmcp:fail (strcat "Entity not found: " handle))
  )
  e
)

(defun zwmcp:opt (key opts default / a)
  (if (setq a (assoc key opts)) (cdr a) default)
)

(defun zwmcp:pt3 (p)
  (list (float (car p)) (float (cadr p)) (if (caddr p) (float (caddr p)) 0.0))
)

;; Entity counts grouped by a DXF group code (8 = layer, 0 = type) using one ssget and a native sort.
;; flt: ssget filter list, e.g. ((410 . "Model") (0 . "LINE,TEXT")), or nil for every entity.
(defun zwmcp:count-by (code flt / ss i names prev n acc)
  (setq ss (if flt (ssget "_X" flt) (ssget "_X")))
  (if (and ss code)
    (progn
      (setq i (sslength ss))
      (while (> i 0)
        (setq i (1- i)
              names (cons (cdr (assoc code (entget (ssname ss i)))) names))
      )
      (setq names (acad_strlsort names) n 0)
      (foreach nm names
        (if (and prev (= (strcase nm) (strcase prev)))
          (setq n (1+ n))
          (progn
            (if prev (setq acc (cons (cons prev n) acc)))
            (setq prev nm n 1)
          )
        )
      )
      (if prev (setq acc (cons (cons prev n) acc)))
    )
  )
  (list (cons "total" (if ss (sslength ss) 0)) (cons "groups" acc))
)

(defun zwmcp:ping ()
  (list (cons "pong" T)
        (cons "unicode" (if zwmcp:*unicode* T 'false))
        (cons "document" (getvar "DWGNAME"))
        (cons "dwgprefix" (getvar "DWGPREFIX"))
        (cons "secureload" (vl-catch-all-apply 'getvar (list "SECURELOAD"))))
)
