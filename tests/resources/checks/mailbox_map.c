/* FA map and owner preflight/commit contracts. SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-components.h"

static const char *mailbox_access(DB2010FaMailbox *m, unsigned offset, unsigned size,
                           bool write, uint32_t value, bool commit)
{
    return dbfm_access(m,0xfa040000+offset,size,write,&value,commit);
}

static void layout(void)
{
    const DB2010RegMap *map=dbfm_map();
    assert(map==dbfm_map() && map->count==2 && map->claim_count==2);
    assert(map->claims[0].address==0xfa040000 && map->claims[0].span==4);
    assert(map->claims[0].widths==DB_WIDTH32 && map->claims[0].alignment==4);
    assert(map->claims[0].readable && map->claims[0].writable);
    assert(map->claims[1].address==0xfa040040 && map->claims[1].span==64);
    assert(map->claims[1].widths==DB_WIDTH16 && map->claims[1].alignment==2);
    assert(!map->claims[1].readable && map->claims[1].writable);
    for(unsigned i=0;i<32;i++) {
        DB2010Access a={.address=0xfa040040+2*i,.size=2,.write=true};
        DB2010DecodedAccess d;
        assert(!db2010_reg_decode(map,&a,&d));
        assert(d.id==DBFM_TX_WORDS && d.element==i && !d.bank);
        assert(d.rule->backing_width==2);
        a.write=false;assert(!db2010_reg_decode(map,&a,&d));
        assert(d.action->kind==DB_ACTION_REJECT);
    }
    assert(!dbfm_contains(0xfa040004,4) && !dbfm_contains(0xfa040080,2));
    assert(!dbfm_contains(0xfa04000c,4) && !dbfm_contains(0xfa040024,4));
    DB2010FaMailbox m={.busy=true};
    assert(!strcmp(mailbox_access(&m,0x40,4,false,0,true),"fa_mailbox_access_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0x40,2,false,0,true),"fa_mailbox_tx_buffer_read_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0x40,2,true,0,true),"fa_mailbox_peer_consumption_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0,4,true,0,true),"fa_mailbox_command_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0,2,true,0,true),"fa_mailbox_access_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0x41,2,true,0,true),"fa_mailbox_access_unmodeled"));
    assert(!strcmp(mailbox_access(&m,0x7e,4,true,0,true),"fa_mailbox_access_unmodeled"));
}

static void lifecycle(void)
{
    DB2010FaMailbox m={0},before=m;
    assert(!strcmp(mailbox_access(&m,0,4,true,1,false),"fa_mailbox_header_unknown"));
    assert(!mailbox_access(&m,0x40,2,true,0x0822,true));
    assert(!strcmp(mailbox_access(&m,0,4,true,1,false),"fa_mailbox_header_unmodeled"));
    assert(!mailbox_access(&m,0x40,2,true,0x0802,true));
    assert(!strcmp(mailbox_access(&m,0,4,true,1,false),"fa_mailbox_payload_unknown"));
    dbfm_reset(&m);assert(!memcmp(&m,&before,sizeof(m)));
    for(unsigned i=0;i<32;i++) {
        uint32_t value=i?0x10000+i:(31u<<11)|2;
        before=m;
        for(unsigned j=0;j<3;j++) {
            assert(!mailbox_access(&m,0x40+2*i,2,true,value,false));
            assert(!memcmp(&m,&before,sizeof(m)));
        }
        assert(!mailbox_access(&m,0x40+2*i,2,true,value,true));
        assert(m.words[i]==(uint16_t)value);
    }
    assert(m.known==UINT32_MAX && !m.busy && !m.submissions);
    before=m;
    for(unsigned j=0;j<3;j++) {
        assert(!mailbox_access(&m,0,4,true,1,false));
        assert(!memcmp(&m,&before,sizeof(m)));
    }
    assert(!mailbox_access(&m,0,4,true,1,true));
    assert(m.busy && m.submissions==1 && m.payload_words==31);
    uint32_t value=0;
    assert(!dbfm_access(&m,0xfa040000,4,false,&value,true) && value==1);
    assert(!strcmp(mailbox_access(&m,0,4,true,1,true),"fa_mailbox_peer_consumption_unmodeled"));
    assert(m.submissions==1);
    dbfm_consumed(&m);assert(!m.busy && m.known==UINT32_MAX);
    assert(!mailbox_access(&m,0,4,true,1,true) && m.submissions==2);
    const DB2010RegMap *map=dbfm_map();
    dbfm_reset(&m);assert(!m.busy && !m.known && !m.submissions);
    assert(dbfm_map()==map);
    DB2010FaMailbox other={0};
    assert(!mailbox_access(&m,0x40,2,true,0x0802,true));
    assert(!other.known); /* Layout is shared, runtime staging is not. */
}

static void peer(void)
{
    DB2010DspPeer p={.config={.latency=4}};
    DB2010Dma dma={0};
    DB2010Register mode={.width=2,.known_mask=0xffff,.value=0x18};
    DB2010FaLink s={.has_dsp_peer=true,.dsp_peer=&p,.dma=&dma,.mode=&mode,
                   .fa_mailbox={.words={0x1814,2,2,112},.known=15}};
    DB2010FaMailbox before=s.fa_mailbox;
    DB2010DspPeerRuntime pb=p.state;
    DB2010Access a={.address=0xfa040000,.size=4,.write=true,.value=1,.now=10,.commit=true};
    assert(!strcmp(db2010_mailbox_access(&s,&a),"dsp_peer_pending_dma_unmodeled"));
    assert(!memcmp(&before,&s.fa_mailbox,sizeof(before)) && !memcmp(&pb,&p.state,sizeof(pb)));
    a.value=0;assert(!strcmp(db2010_mailbox_access(&s,&a),"fa_mailbox_command_unmodeled"));
    dma.state.channel[2][4]=0xc8c7;dma.state.channel[2][1]=0xfa040020;
    dma.state.channel[2][3]=0xa4492038;
    a.value=1;a.commit=false;
    for(unsigned j=0;j<3;j++) {
        assert(!db2010_mailbox_access(&s,&a));
        assert(!memcmp(&before,&s.fa_mailbox,sizeof(before)) && !memcmp(&pb,&p.state,sizeof(pb)));
    }
    a.commit=true;assert(!db2010_mailbox_access(&s,&a));
    assert(s.fa_mailbox.busy && s.fa_mailbox.submissions==1);
    assert(p.state.requests==1 && p.state.phase==1 && p.state.due==14);
    assert(!strcmp(db2010_mailbox_access(&s,&a),"fa_mailbox_peer_consumption_unmodeled"));
    assert(p.state.requests==1 && s.fa_mailbox.submissions==1);
    /* Status reads do not preflight a new peer packet. */
    a.write=false;a.value=0;assert(!db2010_mailbox_access(&s,&a) && a.value==1);
    assert(p.state.requests==1);
}

int main(int argc,char **argv)
{
    assert(argc==2);
    if(!strcmp(argv[1],"layout")) { layout(); }
    else if(!strcmp(argv[1],"lifecycle")) { lifecycle(); }
    else if(!strcmp(argv[1],"peer")) { peer(); }
    else { assert(false); }
    return 0;
}
