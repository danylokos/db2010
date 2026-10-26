/* Shared access contracts: layout, actions, ownership and rejected declarations.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-bus.h"

#define RD(w, v) { .kind=DB_ACTION_CONSTANT, .widths=(w), .constant=(v) }
#define WR(w) { .kind=DB_ACTION_IGNORE, .widths=(w) }
#define DENY(w, why) { .kind=DB_ACTION_REJECT, .widths=(w), .fault=(why) }

static const char *wrapped(void *opaque, DB2010Access *a) { return NULL; }

static DB2010DecodedAccess decode(const DB2010RegMap *m, uint32_t address,
                                  unsigned size, bool write)
{
    DB2010Access a = {.address=address, .size=size, .write=write};
    DB2010DecodedAccess d;
    assert(!db2010_reg_decode(m, &a, &d));
    return d;
}

static void rejected(const DB2010RegMap *m, uint32_t address,
                      unsigned size, bool write)
{
    DB2010Access a = {.address=address, .size=size, .write=write,
                      .fault_address=0xabcdef, .fault_size=7};
    DB2010DecodedAccess d = {0}, before = d;
    assert(!strcmp(db2010_reg_decode(m, &a, &d), "shape"));
    assert(!memcmp(&before, &d, sizeof(d)));
    assert(a.fault_address == 0xabcdef && a.fault_size == 7);
}

static void layouts(void)
{
    static const unsigned reverse[] = {5,4,3,2,1,0};
    static const uint32_t bases[] = {0x10,0x50,0x100};
    static const DB2010RegRule channels[] = {
        DB_REG(0, .id=10, .read=RD(DB_WIDTH32, 1), .write=WR(DB_WIDTH32)),
        DB_REG(4, .id=11, .read=RD(DB_WIDTH32, 2), .write=WR(DB_WIDTH32)),
    };
    static const DB2010RegRule groups[] = {
        DB_REPEAT(0, 8, .stride=0x20, .children=channels, .child_count=2),
    };
    static const DB2010RegRule nonuniform[] = {
        DB_REG(2, .id=12, .read=RD(DB_WIDTH16, 3)),
    };
    static const DB2010RegRule sparse[] = {
        DB_REG(0, .read=RD(DB_WIDTH32, 1)),
        DB_REG(8, .read=RD(DB_WIDTH32, 2)),
    };
    static const DB2010RegRule rules[] = {
        DB_VIEW(0x5c, .read=RD(DB_WIDTH16, 0x1234), .write=WR(DB_WIDTH8)),
        DB_ARRAY(0x2a, 25, .stride=1, .alignment=1, .backing_width=1,
                 .read=RD(DB_WIDTH16, 1), .write=WR(DB_WIDTH16)),
        DB_WINDOW(0x100, 0x100, .children=groups, .child_count=1),
        DB_WINDOW(0x1000, 0x100, .children=sparse, .child_count=2),
        DB_REPEAT(0x2000, 3, .bases=bases, .children=nonuniform, .child_count=1),
        DB_ARRAY(0x3000, 6, .id=13, .stride=4, .selector=reverse,
                 .read=RD(DB_WIDTH32, 0)),
        DB_WINDOW(0x4000, 0x20, .read=RD(DB_WIDTH8|DB_WIDTH16|DB_WIDTH32, 0),
                  .write=WR(DB_WIDTH8|DB_WIDTH16|DB_WIDTH32)),
    };
    DB2010RegMap m = {0};
    assert(!db2010_regmap_init(&m, 0x10000000, rules, G_N_ELEMENTS(rules), "shape"));
    assert(m.count == 25 && m.claim_count == 9);
    decode(&m, 0x1000005c, 2, false);
    decode(&m, 0x1000005c, 1, true);
    rejected(&m, 0x1000005c, 1, false);
    rejected(&m, 0x1000005c, 2, true);
    for (unsigned i=0; i<25; i++) {
        DB2010DecodedAccess d=decode(&m,0x1000002a+i,2,false);
        assert(d.element==i && d.rule->backing_width==1);
        decode(&m,0x1000002a+i,2,true);
        rejected(&m,0x1000002a+i,1,false);
    }
    assert(m.claims[1].span==26);
    rejected(&m,0x10000043,2,false);
    for (unsigned i=0; i<8; i++) {
        DB2010DecodedAccess d=decode(&m,0x10000104+i*0x20,4,false);
        assert(d.id==11 && d.bank==i && !d.element);
        rejected(&m,0x10000108+i*0x20,4,false);
        assert(db2010_claims(m.claims,m.claim_count,0x10000108+i*0x20,4));
    }
    rejected(&m,0x10000200,4,false);
    rejected(&m,0x10001004,4,false);
    rejected(&m,0x10001008,2,false);
    for(unsigned i=0;i<3;i++) {
        DB2010DecodedAccess d=decode(&m,0x10002002+bases[i],2,false);
        assert(d.bank==i && d.id==12);
    }
    DB2010DecodedAccess d=decode(&m,0x10003010,4,false);
    assert(d.id==13 && d.element==4 && d.selector==reverse);
    assert(((const unsigned *)d.selector)[d.element]==1);
    decode(&m,0x1000401c,4,false);
    decode(&m,0x1000401f,1,false);
    rejected(&m,0x1000401f,2,false);
    rejected(&m,0x10004001,2,false);
    rejected(&m,0x10004000,0,false);
    rejected(&m,0x10004000,3,false);
    rejected(&m,0x10004000,32,false);
    rejected(&m,0xffffffff,2,false);
    db2010_regmap_destroy(&m);
    assert(!m.rules && !m.claims && !m.count);
    DB2010RegRule end[] = {DB_REG(0xffffffff, .read=RD(DB_WIDTH8,1))};
    assert(!db2010_regmap_init(&m,0,end,1,"shape"));
    decode(&m,0xffffffff,1,false);
    rejected(&m,0xffffffff,2,false);
    db2010_regmap_destroy(&m);
}

typedef struct State {
    uint16_t value;
    uint32_t known, wide, wide_known;
    uint8_t cells[2];
    unsigned callbacks;
    bool busy;
} State;

static const char *field(void *opaque, const DB2010DecodedAccess *d,
                          const DB2010Access *a, DB2010FieldRef *ref)
{
    State *s=opaque;
    if (d->id==1) {
        *ref=(DB2010FieldRef){&s->value,&s->known,0xffff};
    } else if (d->id==2) {
        *ref=(DB2010FieldRef){&s->cells[d->element],NULL,0xff};
    } else {
        *ref=(DB2010FieldRef){&s->wide,&s->wide_known,0xff};
    }
    return NULL;
}

static const char *guard(const void *opaque, const DB2010DecodedAccess *d,
                          const DB2010Access *a)
{
    const State *s=opaque;
    return s->busy ? "busy" : NULL;
}

static const char *callback(void *opaque, const DB2010DecodedAccess *d,
                             DB2010Access *a)
{
    State *s=opaque;
    if (!a->write) { a->value=42; }
    if (a->commit) { s->callbacks++; }
    return NULL;
}

static const char *execute(DB2010RegMap *m, State *s, unsigned address,
                            unsigned size, bool write, uint32_t *value, bool commit)
{
    DB2010Access a={.address=address,.size=size,.write=write,.value=*value,.commit=commit};
    DB2010DecodedAccess d;
    const char *fault=db2010_reg_decode(m,&a,&d);
    if (!fault) { fault=db2010_reg_execute(s,&d,&a); }
    *value=a.value;
    return fault;
}

static void actions(void)
{
    static const uint32_t allowed[]={1,3};
    static const DB2010RegRule rules[]={
        DB_VIEW(0, .id=1, .backing_width=2,
            .read={.kind=DB_ACTION_FIELD,.widths=DB_WIDTH8|DB_WIDTH16,
                   .field=field,.fault="unknown"},
            .write={.kind=DB_ACTION_FIELD,.widths=DB_WIDTH8|DB_WIDTH16,
                    .field=field,.guard=guard,.fault="unknown"}),
        DB_REG(4,.backing_width=4,
            .read={.kind=DB_ACTION_READ_CLEAR,.widths=DB_WIDTH32,.field=field,.fault="unknown"}),
        DB_REG(8,.backing_width=4,
            .write={.kind=DB_ACTION_W1C,.widths=DB_WIDTH32,.field=field,.fault="unknown"}),
        DB_REG(12,.read=RD(DB_WIDTH32,7),
            .write={.kind=DB_ACTION_IGNORE,.widths=DB_WIDTH32,.guard=guard,
                .constraint={.kind=DB_VALUE_SET,.values=allowed,.count=2,.fault="value"}}),
        DB_REG(16,.write={.kind=DB_ACTION_IGNORE,.widths=DB_WIDTH32,
            .constraint={.kind=DB_VALUE_MASKED,.mask=0xf0,.value=0xa0,.fault="mask"}}),
        DB_ARRAY(20,2,.id=2,.stride=1,.alignment=1,.backing_width=1,
            .read={.kind=DB_ACTION_FIELD,.widths=DB_WIDTH16,.field=field,.fault="unknown"},
            .write={.kind=DB_ACTION_FIELD,.widths=DB_WIDTH16,.field=field,.fault="unknown",
                .constraint={.kind=DB_VALUE_RANGE,.maximum=255,.fault="upper"}}),
        DB_REG(32,.read={.kind=DB_ACTION_CALLBACK,.widths=DB_WIDTH32,.callback=callback},
                  .write={.kind=DB_ACTION_CALLBACK,.widths=DB_WIDTH32,.callback=callback}),
        DB_WINDOW(48,8,.read={.kind=DB_ACTION_CONSTANT,
            .widths=DB_WIDTH8|DB_WIDTH16|DB_WIDTH32,.constant=UINT32_MAX,.constant_by_width=true}),
        DB_REG(64,.read=DENY(DB_WIDTH16,"direction"),.write=WR(DB_WIDTH16)),
    };
    DB2010RegMap m={0};
    assert(!db2010_regmap_init(&m,0,rules,G_N_ELEMENTS(rules),"shape"));
    State s={.value=0xa55a};uint32_t v=0;
    assert(!strcmp(execute(&m,&s,0,1,false,&v,false),"unknown"));
    State before=s;v=0x33;
    for(unsigned i=0;i<3;i++) {
        assert(!execute(&m,&s,0,1,true,&v,false));
        assert(!memcmp(&s,&before,sizeof(s)));
    }
    assert(!execute(&m,&s,0,1,true,&v,true));
    assert(s.value==0xa533 && s.known==0xff);
    assert(!execute(&m,&s,0,1,false,&v,true) && v==0x33);
    assert(!strcmp(execute(&m,&s,0,2,false,&v,false),"unknown"));
    v=0xbeef;assert(!execute(&m,&s,0,2,true,&v,true));
    assert(s.value==0xbeef && s.known==0xffff);
    s.busy=true;
    DB2010DecodedAccess d=decode(&m,0,2,true); /* No runtime predicate cached. */
    DB2010Access a={.address=0,.size=2,.write=true,.value=0x9876,.commit=true};
    assert(!strcmp(db2010_reg_execute(&s,&d,&a),"busy"));
    s.busy=false;assert(!db2010_reg_execute(&s,&d,&a) && s.value==0x9876);
    a.address=2;assert(!strcmp(db2010_reg_execute(&s,&d,&a),"decoded register access mismatch"));
    s.wide=0x123456a5;s.wide_known=0xff;before=s;
    for(unsigned i=0;i<3;i++) {
        assert(!execute(&m,&s,4,4,false,&v,false) && v==0xa5);
        assert(!memcmp(&s,&before,sizeof(s)));
    }
    assert(!execute(&m,&s,4,4,false,&v,true) && v==0xa5);
    assert(s.wide==0x12345600);
    s.wide=0xabcdefab;v=0x81;before=s;
    assert(!execute(&m,&s,8,4,true,&v,false) && !memcmp(&s,&before,sizeof(s)));
    assert(!execute(&m,&s,8,4,true,&v,true) && s.wide==0xabcdef2a);
    s.wide_known=0;before=s;
    assert(!strcmp(execute(&m,&s,8,4,true,&v,true),"unknown"));
    assert(!memcmp(&s,&before,sizeof(s)));
    s.busy=true;v=2;
    assert(!strcmp(execute(&m,&s,12,4,true,&v,true),"value"));
    v=1;assert(!strcmp(execute(&m,&s,12,4,true,&v,true),"busy"));
    s.busy=false;assert(!execute(&m,&s,12,4,true,&v,true));
    v=0xa5;assert(!execute(&m,&s,16,4,true,&v,true));
    v=0x95;assert(!strcmp(execute(&m,&s,16,4,true,&v,true),"mask"));
    v=0x100;assert(!strcmp(execute(&m,&s,20,2,true,&v,true),"upper"));
    v=0xab;assert(!execute(&m,&s,21,2,true,&v,true));
    assert(s.cells[0]==0 && s.cells[1]==0xab);
    assert(!execute(&m,&s,21,2,false,&v,true) && v==0xab);
    for(unsigned i=0;i<3;i++) {
        assert(!execute(&m,&s,32,4,false,&v,false) && v==42);
        assert(!s.callbacks);
    }
    assert(!execute(&m,&s,32,4,false,&v,true) && s.callbacks==1);
    for(unsigned size=1;size<=4;size*=2) {
        assert(!execute(&m,&s,48,size,false,&v,true));
        assert(v==(size==4?UINT32_MAX:(1u<<(8*size))-1));
    }
    s.busy=true;
    assert(!strcmp(execute(&m,&s,64,2,false,&v,true),"direction"));
    assert(!m.claims[m.claim_count-1].readable);
    /* Reset of owner state cannot leave cached values/knowledge/guards. */
    s=(State){0};v=0;
    assert(!strcmp(execute(&m,&s,0,2,false,&v,true),"unknown"));
    db2010_regmap_destroy(&m);
}

static void ownership(void)
{
    DB2010RegRule rules[]={
        DB_REG(0x8000,.read=RD(DB_WIDTH32,1)),
        DB_WINDOW(0x9000,0x100,.read=RD(DB_WIDTH8,1)),
    };
    DB2010RegMap m={0};DB2010Bus bus;
    assert(!db2010_regmap_init(&m,0,rules,2,"shape"));
    db2010_bus_init(&bus);
    DB2010RegisterSpec legacy={0x7000,4,DB_WIDTH32,4,true,true};
    assert(!db2010_bus_add(&bus,"legacy",&legacy,1,wrapped,NULL,3));
    assert(!db2010_bus_add_map(&bus,"map",&m,wrapped,&m,2));
    DB2010Owner *owner=db2010_bus_owner(&bus,0x9080,1);
    assert(owner && owner->opaque==&m && owner->access==wrapped);
    assert(owner->claims==m.claims && owner->count==m.claim_count);
    assert(db2010_bus_owner(&bus,0x7000,4)!=owner);
    unsigned count=bus.owners->len;
    DB2010RegRule clash[]={DB_REG(0x6000,.read=RD(DB_WIDTH32,0)),
        DB_REG(0x7000,.read=RD(DB_WIDTH32,0))};
    DB2010RegMap bad={0};assert(!db2010_regmap_init(&bad,0,clash,2,"shape"));
    assert(!strcmp(db2010_bus_add_map(&bus,"clash",&bad,wrapped,NULL,1),"conflicting MMIO ownership"));
    assert(bus.owners->len==count && !db2010_bus_owner(&bus,0x6000,4));
    assert(!db2010_bus_owner(&bus,0x8000,0));
    DB2010RegisterSpec adjacent={0x8004,4,DB_WIDTH32,4,true,true};
    assert(!db2010_bus_add(&bus,"adjacent",&adjacent,1,wrapped,NULL,1));
    assert(!strcmp(db2010_bus_owner(&bus,0x8000,8)->name,"adjacent"));
    db2010_bus_destroy(&bus);db2010_regmap_destroy(&m);db2010_regmap_destroy(&bad);
}

static void invalid(void)
{
    DB2010RegMap m={0};
    DB2010Access a={.size=4};DB2010DecodedAccess d;
    assert(!strcmp(db2010_reg_decode(&m,&a,&d),"uninitialized register map"));
    DB2010RegRule overlap[]={DB_REG(0,.read=RD(DB_WIDTH32,0)),
        DB_REG(2,.read=RD(DB_WIDTH16,0))};
    assert(db2010_regmap_init(&m,0,overlap,2,"shape"));
    assert(!m.rules && !m.claims);
    DB2010RegRule child[]={DB_REG(0,.read=RD(DB_WIDTH32,0))};
    DB2010RegRule envelope[]={DB_WINDOW(0,16,.children=overlap,.child_count=2)};
    assert(db2010_regmap_init(&m,0,envelope,1,"shape"));
    DB2010RegRule cycle=DB_REPEAT(0,1,.child_count=1);
    cycle.children=&cycle;
    assert(db2010_regmap_init(&m,0,&cycle,1,"shape"));
    DB2010RegRule overflow[]={DB_ARRAY(0xfffffffe,2,.stride=2,.read=RD(DB_WIDTH16,0))};
    assert(db2010_regmap_init(&m,0,overflow,1,"shape"));
    DB2010RegRule repeated[]={DB_REPEAT(0xfffffff0,8,.stride=0x20,.children=child,.child_count=1)};
    assert(db2010_regmap_init(&m,0,repeated,1,"shape"));
    DB2010RegRule outside[]={DB_WINDOW(0,2,.children=child,.child_count=1)};
    assert(db2010_regmap_init(&m,0,outside,1,"shape"));
    DB2010RegRule zero[]={DB_ARRAY(0,0,.stride=2,.read=RD(DB_WIDTH16,0))};
    assert(db2010_regmap_init(&m,0,zero,1,"shape"));
    DB2010RegRule badwidth[]={DB_REG(0,.read=RD(1u<<3,0))};
    assert(db2010_regmap_init(&m,0,badwidth,1,"shape"));
    DB2010RegRule badalign[]={DB_REG(0,.alignment=3,.read=RD(DB_WIDTH32,0))};
    assert(db2010_regmap_init(&m,0,badalign,1,"shape"));
    DB2010RegRule badbacking[]={DB_REG(0,.backing_width=3,
        .read={.kind=DB_ACTION_FIELD,.widths=DB_WIDTH32,.field=field,.fault="unknown"})};
    assert(db2010_regmap_init(&m,0,badbacking,1,"shape"));
    DB2010RegRule badconstraint[]={DB_REG(0,.write={.kind=DB_ACTION_IGNORE,.widths=DB_WIDTH32,
        .constraint={.kind=DB_VALUE_RANGE,.minimum=2,.maximum=1,.fault="value"}})};
    assert(db2010_regmap_init(&m,0,badconstraint,1,"shape"));
    DB2010RegRule emptywindow[]={DB_WINDOW(0,16)};
    assert(!db2010_regmap_init(&m,0,emptywindow,1,"shape"));
    assert(!m.count && m.claim_count==1);
    rejected(&m,0,1,false);
    db2010_regmap_destroy(&m);
    assert(!m.rules && !m.claims);
}

int main(int argc, char **argv)
{
    assert(argc==2);
    if (!strcmp(argv[1],"layouts")) { layouts(); }
    else if (!strcmp(argv[1],"actions")) { actions(); }
    else if (!strcmp(argv[1],"ownership")) { ownership(); }
    else if (!strcmp(argv[1],"invalid")) { invalid(); }
    else { assert(false); }
    return 0;
}
